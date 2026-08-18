// omni-machine-watch holds a long-lived COSI watch on
// MachineStatuses.omni.sidero.dev and emits one JSON object per line on
// stdout, so an agent driving a cluster build can follow machine state
// without polling.
//
// omnictl cannot serve this: its `get --watch` prints a human table and
// terminates with the command, whereas a factory run needs a process whose
// lifetime the caller owns. The COSI Go client is the supported way to hold
// that subscription (siderolabs/omni client/pkg/client/example_test.go).
//
// Credentials come from the environment only -- OMNI_ENDPOINT and
// OMNI_SERVICE_ACCOUNT_KEY, the same pair talosctl's Omni auth reads. The key
// is deliberately not a flag: flags land in `ps` output and shell history.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"runtime/debug"
	"syscall"
	"time"

	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"

	"github.com/siderolabs/omni/client/pkg/client"
	"github.com/siderolabs/omni/client/pkg/omni/resources/omni"
)

// omniClientModule is the module whose COSI client this binary is built
// against. Reported by -version from the embedded build info rather than
// from a constant, so the number cannot drift from what was linked in.
const omniClientModule = "github.com/siderolabs/omni/client"

// event is one line of the output stream. Fields are omitempty so a
// destroyed/lifecycle event stays readable, but connected/maintenance are
// pointers: `false` is meaningful for both and must not be elided.
type event struct {
	TS           string  `json:"ts"`
	Event        string  `json:"event"`
	ID           string  `json:"id,omitempty"`
	Version      string  `json:"version,omitempty"`
	Connected    *bool   `json:"connected,omitempty"`
	Maintenance  *bool   `json:"maintenance,omitempty"`
	Cluster      string  `json:"cluster,omitempty"`
	Hostname     string  `json:"hostname,omitempty"`
	Role         string  `json:"role,omitempty"`
	TalosVersion string  `json:"talos_version,omitempty"`
	LastError    string  `json:"last_error,omitempty"`
	Message      string  `json:"message,omitempty"`
	Attempt      int     `json:"attempt,omitempty"`
	BackoffSec   float64 `json:"backoff_sec,omitempty"`
}

type emitter struct {
	enc *json.Encoder
}

func newEmitter(w io.Writer) *emitter {
	return &emitter{enc: json.NewEncoder(w)}
}

func (e *emitter) emit(ev event) {
	ev.TS = time.Now().UTC().Format(time.RFC3339Nano)

	if err := e.enc.Encode(ev); err != nil {
		fmt.Fprintf(os.Stderr, "omni-machine-watch: failed to encode event: %v\n", err)
	}
}

func main() {
	endpoint := flag.String("endpoint", os.Getenv("OMNI_ENDPOINT"),
		"Omni gRPC endpoint (default $OMNI_ENDPOINT). Must be a direct gRPC path -- a Cloudflare Tunnel in front of it breaks gRPC trailers.")
	bootstrap := flag.Bool("bootstrap", true,
		"emit the current contents of the kind before streaming further changes")
	retryMin := flag.Duration("retry-min", time.Second, "initial reconnect backoff")
	retryMax := flag.Duration("retry-max", time.Minute, "maximum reconnect backoff")
	showVersion := flag.Bool("version", false, "print the build's Omni client and Go versions, then exit")
	flag.Parse()

	if *showVersion {
		fmt.Println(versionString(debug.ReadBuildInfo()))

		return
	}

	if *endpoint == "" {
		fmt.Fprintln(os.Stderr, "omni-machine-watch: no endpoint: set OMNI_ENDPOINT or pass -endpoint")
		os.Exit(2)
	}

	serviceAccountKey := os.Getenv("OMNI_SERVICE_ACCOUNT_KEY")
	if serviceAccountKey == "" {
		fmt.Fprintln(os.Stderr, "omni-machine-watch: OMNI_SERVICE_ACCOUNT_KEY is not set")
		os.Exit(2)
	}

	// The watch's lifetime is the caller's: SIGINT/SIGTERM cancels the
	// context, which tears the subscription down and returns cleanly rather
	// than leaving a half-closed stream on the server.
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()

	if err := run(ctx, *endpoint, serviceAccountKey, *bootstrap, *retryMin, *retryMax); err != nil {
		fmt.Fprintf(os.Stderr, "omni-machine-watch: %v\n", err)
		os.Exit(1)
	}
}

// run reconnects for as long as the context is alive. A watch against a
// remote Omni will eventually be dropped (restart, LB timeout, network
// blip); a factory run that outlives that has to re-establish rather than
// exit, so the caller sees a `reconnect` line and a fresh bootstrap instead
// of silence.
func run(ctx context.Context, endpoint, serviceAccountKey string, bootstrap bool, retryMin, retryMax time.Duration) error {
	out := newEmitter(os.Stdout)
	backoff := retryMin

	for attempt := 1; ; attempt++ {
		err := watchOnce(ctx, endpoint, serviceAccountKey, bootstrap, out)

		if ctx.Err() != nil {
			out.emit(event{Event: "stopped", Message: "context canceled"})

			return nil
		}

		out.emit(event{
			Event:      "reconnect",
			Message:    errString(err),
			Attempt:    attempt,
			BackoffSec: backoff.Seconds(),
		})

		select {
		case <-ctx.Done():
			out.emit(event{Event: "stopped", Message: "context canceled"})

			return nil
		case <-time.After(backoff):
		}

		if backoff *= 2; backoff > retryMax {
			backoff = retryMax
		}
	}
}

// watchOnce holds one subscription and returns when it breaks or the context
// is canceled.
func watchOnce(ctx context.Context, endpoint, serviceAccountKey string, bootstrap bool, out *emitter) error {
	c, err := client.New(endpoint, client.WithServiceAccount(serviceAccountKey))
	if err != nil {
		return fmt.Errorf("failed to create omni client: %w", err)
	}

	defer func() {
		if closeErr := c.Close(); closeErr != nil {
			fmt.Fprintf(os.Stderr, "omni-machine-watch: failed to close client: %v\n", closeErr)
		}
	}()

	out.emit(event{Event: "watching", Message: endpoint})

	return stream(ctx, c.Omni().State(), bootstrap, out)
}

// stream subscribes to the MachineStatus kind on st and pumps events into out
// until the subscription breaks or the context is canceled. It takes a
// state.CoreState rather than a client so it can be driven against an
// in-memory COSI state in tests -- the live watch only ever proves the
// bootstrap path unless a machine happens to change while it is running.
func stream(ctx context.Context, st state.CoreState, bootstrap bool, out *emitter) error {
	watchCtx, watchCancel := context.WithCancel(ctx)
	defer watchCancel()

	eventCh := make(chan safe.WrappedStateEvent[*omni.MachineStatus])

	opts := []state.WatchKindOption{}
	if bootstrap {
		opts = append(opts, state.WithBootstrapContents(true))
	}

	if err := safe.StateWatchKind(watchCtx, st, omni.NewMachineStatus("").Metadata(), eventCh, opts...); err != nil {
		return fmt.Errorf("failed to watch machine statuses: %w", err)
	}

	for {
		select {
		case <-watchCtx.Done():
			return watchCtx.Err()
		case ev := <-eventCh:
			if err := handle(ev, out); err != nil {
				return err
			}
		}
	}
}

// handle turns one watch event into one output line. Only state.Errored ends
// the subscription; everything else is data.
func handle(ev safe.WrappedStateEvent[*omni.MachineStatus], out *emitter) error {
	switch ev.Type() {
	case state.Errored:
		return fmt.Errorf("watch errored: %w", ev.Error())
	case state.Bootstrapped:
		// End of the initial contents dump: everything after this line is a
		// live change, which is what a "wait until N machines appear" caller
		// needs to distinguish.
		out.emit(event{Event: "bootstrapped"})

		return nil
	case state.Noop:
		return nil
	case state.Created, state.Updated, state.Destroyed:
	}

	res, err := ev.Resource()
	if err != nil {
		return fmt.Errorf("failed to decode machine status: %w", err)
	}

	spec := res.TypedSpec().Value
	connected := spec.GetConnected()
	maintenance := spec.GetMaintenance()

	out.emit(event{
		Event:        eventName(ev.Type()),
		ID:           res.Metadata().ID(),
		Version:      res.Metadata().Version().String(),
		Connected:    &connected,
		Maintenance:  &maintenance,
		Cluster:      spec.GetCluster(),
		Hostname:     spec.GetNetwork().GetHostname(),
		Role:         spec.GetRole().String(),
		TalosVersion: spec.GetTalosVersion(),
		LastError:    spec.GetLastError(),
	})

	return nil
}

func eventName(t state.EventType) string {
	switch t {
	case state.Created:
		return "created"
	case state.Updated:
		return "updated"
	case state.Destroyed:
		return "destroyed"
	case state.Bootstrapped:
		return "bootstrapped"
	case state.Errored:
		return "errored"
	case state.Noop:
		return "noop"
	default:
		return "unknown"
	}
}

// versionString reports what this binary was actually linked against. Taking
// it from the build info rather than a build flag means it cannot disagree
// with the module that was compiled in, which is the whole point of a
// version probe.
func versionString(bi *debug.BuildInfo, ok bool) string {
	if !ok || bi == nil {
		return "omni-machine-watch (build info unavailable)"
	}

	client := "unknown"

	for _, dep := range bi.Deps {
		if dep.Path != omniClientModule {
			continue
		}

		client = dep.Version
		if dep.Replace != nil {
			client = dep.Replace.Version
		}

		break
	}

	return fmt.Sprintf("omni-machine-watch (%s %s, %s)", omniClientModule, client, bi.GoVersion)
}

func errString(err error) string {
	if err == nil || errors.Is(err, context.Canceled) {
		return ""
	}

	return err.Error()
}
