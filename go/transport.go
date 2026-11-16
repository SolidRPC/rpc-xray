package rpcxray

import (
	"context"
	"errors"
	"io"
	"net"
	"net/http"
	"strings"
)

type Transport struct {
	Xray  *Xray
	Chain string
	Base  http.RoundTripper
}

func failureKind(err error) string {
	var timeout net.Error
	if errors.Is(err, context.DeadlineExceeded) || errors.Is(err, context.Canceled) || (errors.As(err, &timeout) && timeout.Timeout()) {
		return "timeout"
	}
	return "network"
}

func (t *Transport) begin(r *http.Request) (o *Observation) {
	defer func() {
		if recover() != nil {
			o = nil
		}
	}()
	if t.Xray == nil || r.Method != http.MethodPost || r.GetBody == nil || r.ContentLength > int64(t.Xray.options.MaxBodyBytes) {
		return nil
	}
	copy, err := r.GetBody()
	if err != nil {
		return nil
	}
	defer copy.Close()
	body, err := io.ReadAll(io.LimitReader(copy, int64(t.Xray.options.MaxBodyBytes)+1))
	if err != nil {
		return nil
	}
	return t.Xray.Start(r.URL.String(), t.Chain, body)
}

func (t *Transport) RoundTrip(r *http.Request) (*http.Response, error) {
	base := t.Base
	if base == nil {
		base = http.DefaultTransport
	}
	o := t.begin(r)
	response, err := base.RoundTrip(r)
	if err != nil {
		o.Finish(0, nil, failureKind(err))
		return response, err
	}
	if o == nil || response == nil {
		return response, err
	}
	encoding := strings.ToLower(response.Header.Get("Content-Encoding"))
	if response.StatusCode >= 400 || response.ContentLength > int64(t.Xray.options.MaxBodyBytes) || (encoding != "" && encoding != "identity") || response.Body == nil {
		o.Finish(response.StatusCode, nil, "")
	} else {
		response.Body = &bodyCapture{inner: response.Body, observation: o, status: response.StatusCode, cap: t.Xray.options.MaxBodyBytes, body: make([]byte, 0), remaining: response.ContentLength}
	}
	return response, err
}

func (t *Transport) CloseIdleConnections() {
	base := t.Base
	if base == nil {
		base = http.DefaultTransport
	}
	if closer, ok := base.(interface{ CloseIdleConnections() }); ok {
		closer.CloseIdleConnections()
	}
}

type bodyCapture struct {
	inner       io.ReadCloser
	observation *Observation
	status, cap int
	body        []byte
	done        bool
	remaining   int64
}

func (b *bodyCapture) Read(p []byte) (int, error) {
	n, err := b.inner.Read(p)
	if !b.done {
		if len(b.body)+n > b.cap {
			b.done = true
			b.body = nil
			b.observation.Finish(b.status, nil, "")
		} else {
			b.body = append(b.body, p[:n]...)
		}
		if !b.done {
			if b.remaining >= 0 {
				b.remaining -= int64(n)
			}
			if err != nil && err != io.EOF {
				b.done = true
				b.observation.Finish(b.status, nil, failureKind(err))
			} else if err == io.EOF || b.remaining == 0 {
				b.done = true
				b.observation.Finish(b.status, b.body, "")
			}
		}
	}
	return n, err
}
func (b *bodyCapture) Close() error {
	err := b.inner.Close()
	if !b.done {
		b.done = true
		b.observation.Finish(b.status, nil, "")
	}
	return err
}

func (x *Xray) Handler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		w.Header().Set("Content-Type", ContentType)
		_, _ = io.WriteString(w, x.Metrics())
	})
}
