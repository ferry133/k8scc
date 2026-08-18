package main

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"runtime/debug"
	"strings"
	"testing"
	"time"

	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"

	"github.com/siderolabs/omni/client/pkg/omni/resources/omni"
)

// TestStreamLifecycle drives the watch against an in-memory COSI state.
//
// This exists because a live watch against a real Omni only proves the
// bootstrap path: an idle instance emits no changes, so "no updated events"
// there is indistinguishable from "the streaming path after bootstrap is
// broken". Here the transitions are forced, so the distinction is decidable.
func TestStreamLifecycle(t *testing.T) {
	ctx, cancel := context.WithTimeout(t.Context(), 30*time.Second)
	defer cancel()

	st := state.WrapCore(namespaced.NewState(inmem.Build))

	// One machine already present, so the bootstrap dump has something in it.
	existing := omni.NewMachineStatus("11111111-1111-1111-1111-111111111111")
	existing.TypedSpec().Value.Connected = true
	existing.TypedSpec().Value.Maintenance = true
	existing.TypedSpec().Value.TalosVersion = "v1.13.8"

	if err := st.Create(ctx, existing); err != nil {
		t.Fatalf("failed to seed machine status: %v", err)
	}

	events, streamErr := runStream(ctx, t, st)

	if ev := next(t, events); ev.Event != "created" || ev.ID != existing.Metadata().ID() {
		t.Fatalf("expected bootstrap `created` for the seeded machine, got %+v", ev)
	} else if ev.Connected == nil || !*ev.Connected || ev.Maintenance == nil || !*ev.Maintenance {
		t.Fatalf("spec fields not carried onto the event: %+v", ev)
	} else if ev.TalosVersion != "v1.13.8" {
		t.Fatalf("talos_version not carried onto the event: %+v", ev)
	}

	// Everything before `bootstrapped` is existing contents; everything after
	// is a live change. A caller waiting for a machine to appear depends on
	// that boundary being emitted.
	if ev := next(t, events); ev.Event != "bootstrapped" {
		t.Fatalf("expected `bootstrapped`, got %+v", ev)
	}

	added := omni.NewMachineStatus("22222222-2222-2222-2222-222222222222")
	added.TypedSpec().Value.Cluster = "factory-test"

	if err := st.Create(ctx, added); err != nil {
		t.Fatalf("failed to create machine status: %v", err)
	}

	if ev := next(t, events); ev.Event != "created" || ev.Cluster != "factory-test" {
		t.Fatalf("expected live `created`, got %+v", ev)
	}

	// connected true -> false is the transition a factory run actually waits
	// on, and `false` must survive JSON encoding rather than being elided.
	if _, err := safe.StateUpdateWithConflicts(ctx, st, existing.Metadata(),
		func(res *omni.MachineStatus) error {
			res.TypedSpec().Value.Connected = false

			return nil
		}); err != nil {
		t.Fatalf("failed to update machine status: %v", err)
	}

	if ev := next(t, events); ev.Event != "updated" {
		t.Fatalf("expected `updated`, got %+v", ev)
	} else if ev.Connected == nil {
		t.Fatalf("connected:false was elided from the event: %+v", ev)
	} else if *ev.Connected {
		t.Fatalf("expected connected:false, got %+v", ev)
	}

	if err := st.Destroy(ctx, added.Metadata()); err != nil {
		t.Fatalf("failed to destroy machine status: %v", err)
	}

	if ev := next(t, events); ev.Event != "destroyed" || ev.ID != added.Metadata().ID() {
		t.Fatalf("expected `destroyed`, got %+v", ev)
	}

	// The caller owns the lifetime: cancelling the context must end the
	// stream rather than leave it running.
	cancel()

	select {
	case err := <-streamErr:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("expected context.Canceled after cancel, got %v", err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("stream did not return after the context was canceled")
	}
}

// runStream starts stream() against st and decodes its output into a channel.
func runStream(ctx context.Context, t *testing.T, st state.CoreState) (<-chan event, <-chan error) {
	t.Helper()

	reader, writer := io.Pipe()
	events := make(chan event, 64)
	streamErr := make(chan error, 1)

	go func() {
		defer writer.Close() //nolint:errcheck

		streamErr <- stream(ctx, st, true, newEmitter(writer))
	}()

	go func() {
		defer close(events)

		dec := json.NewDecoder(reader)

		for {
			var ev event

			if err := dec.Decode(&ev); err != nil {
				return
			}

			events <- ev
		}
	}()

	return events, streamErr
}

func next(t *testing.T, events <-chan event) event {
	t.Helper()

	select {
	case ev, ok := <-events:
		if !ok {
			t.Fatal("event stream closed early")
		}

		return ev
	case <-time.After(10 * time.Second):
		t.Fatal("timed out waiting for an event")

		return event{}
	}
}

// TestVersionStringReportsLinkedOmniClient guards the -version flag the image
// build asserts against. It reads the module version out of the embedded
// build info, so a stale or mismatched client cannot be reported as the
// pinned one -- which is the only reason to have the flag at all.
func TestVersionStringReportsLinkedOmniClient(t *testing.T) {
	got := versionString(debug.ReadBuildInfo())

	if !strings.Contains(got, omniClientModule) {
		t.Fatalf("version string does not name the Omni client module: %q", got)
	}

	// A test binary that reported "unknown" would still contain the module
	// path, so the version itself has to be checked separately.
	if strings.Contains(got, omniClientModule+" unknown") {
		t.Fatalf("Omni client version not resolved from build info: %q", got)
	}

	if !strings.Contains(got, omniClientModule+" v") {
		t.Fatalf("Omni client version is not a semver-looking string: %q", got)
	}
}

func TestVersionStringWithoutBuildInfo(t *testing.T) {
	if got := versionString(nil, false); !strings.Contains(got, "unavailable") {
		t.Fatalf("expected an explicit unavailable marker, got %q", got)
	}
}
