package rpcxray

import (
	"fmt"
	"math"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

const Version = "0.1.0"
const ContentType = "text/plain; version=0.0.4; charset=utf-8"

type Options struct {
	MaxBodyBytes    int
	ValuePerRequest *float64
	RevenuePerHour  *float64
}
type labels struct{ chain, provider, method, outcome string }
type counterKey struct {
	name   string
	labels labels
}
type histogram struct {
	count   uint64
	sum     float64
	buckets []uint64
}
type headState struct {
	block    uint64
	advanced time.Time
}
type Xray struct {
	mu         sync.Mutex
	options    Options
	endpoints  map[string]labels
	counters   map[counterKey]float64
	histograms map[labels]*histogram
	heads      map[labels]headState
}

func New(options Options) *Xray {
	if options.MaxBodyBytes <= 0 {
		options.MaxBodyBytes = 1024 * 1024
	}
	return &Xray{options: options, endpoints: map[string]labels{}, counters: map[counterKey]float64{}, histograms: map[labels]*histogram{}, heads: map[labels]headState{}}
}

func (x *Xray) endpoint(rawURL, chain string) (labels, bool) {
	if chain == "" {
		chain = "unknown"
	}
	key := chain + "\x00" + rawURL
	if base, ok := x.endpoints[key]; ok {
		return base, true
	}
	if len(x.endpoints) >= 32 {
		return labels{chain: "unknown", provider: "other"}, true
	}
	u, err := url.Parse(rawURL)
	if err != nil || u.Host == "" || (u.Scheme != "http" && u.Scheme != "https") {
		return labels{}, false
	}
	host := strings.ToLower(u.Host)
	if (u.Scheme == "http" && u.Port() == "80") || (u.Scheme == "https" && u.Port() == "443") {
		host = strings.ToLower(u.Hostname())
		if strings.Contains(host, ":") {
			host = "[" + host + "]"
		}
	}
	base := labels{chain: chain, provider: host}
	x.endpoints[key] = base
	return base, true
}

func (x *Xray) inc(name string, base labels, value float64) {
	key := counterKey{name, base}
	if _, exists := x.counters[key]; exists || len(x.counters)+len(x.histograms) < 5000 {
		x.counters[key] += value
	}
}

type Observation struct {
	x       *Xray
	base    labels
	request *request
	started time.Time
	once    sync.Once
}

func (x *Xray) Start(rawURL, chain string, body []byte) (observation *Observation) {
	defer func() {
		if recover() != nil {
			observation = nil
		}
	}()
	if len(body) > x.options.MaxBodyBytes {
		return nil
	}
	r := parseRequest(body)
	if r == nil {
		return nil
	}
	x.mu.Lock()
	defer x.mu.Unlock()
	base, ok := x.endpoint(rawURL, chain)
	if !ok {
		return nil
	}
	return &Observation{x: x, base: base, request: r, started: time.Now()}
}

func (o *Observation) Finish(status int, body []byte, failure string) {
	if o == nil {
		return
	}
	defer func() { _ = recover() }()
	o.once.Do(func() {
		x := o.x
		if len(body) > x.options.MaxBodyBytes {
			body = nil
		}
		entries := classify(o.request, status, body, failure)
		seconds := time.Since(o.started).Seconds()
		x.mu.Lock()
		defer x.mu.Unlock()
		for _, entry := range entries {
			base := o.base
			base.method, base.outcome = entry.Method, entry.Outcome
			x.inc("rpc_xray_requests_total", base, 1)
		}
		if len(entries) == 0 {
			return
		}
		base := o.base
		base.method = entries[0].Method
		if o.request.batch {
			base.method = "batch"
		}
		h := x.histograms[base]
		if h == nil && len(x.counters)+len(x.histograms) < 5000 {
			h = &histogram{buckets: make([]uint64, len(contract.Buckets))}
			x.histograms[base] = h
		}
		if h != nil {
			h.count++
			h.sum += seconds
			for i, bound := range contract.Buckets {
				if seconds <= bound {
					h.buckets[i]++
				}
			}
		}
		if seconds > 1 {
			x.inc("rpc_xray_slow_seconds_total", o.base, seconds-1)
		}
	})
}

func (x *Xray) RecordProbe(rawURL, chain string, head *uint64) {
	defer func() { _ = recover() }()
	x.mu.Lock()
	defer x.mu.Unlock()
	base, ok := x.endpoint(rawURL, chain)
	if !ok {
		return
	}
	result := base
	result.outcome = "error"
	if head != nil {
		result.outcome = "ok"
		if base.provider != "other" {
			previous, exists := x.heads[base]
			if !exists || *head > previous.block {
				x.heads[base] = headState{*head, time.Now()}
			}
		}
	}
	x.inc("rpc_xray_probe_requests_total", result, 1)
}

func escape(value string) string {
	return strings.NewReplacer("\\", "\\\\", "\n", "\\n", "\"", "\\\"").Replace(value)
}
func renderLabels(base labels, extraKey, extraValue string) string {
	parts := []string{}
	for _, pair := range [][2]string{{"chain", base.chain}, {"provider", base.provider}, {"method", base.method}, {"outcome", base.outcome}, {extraKey, extraValue}} {
		if pair[0] != "" && pair[1] != "" {
			parts = append(parts, pair[0]+"=\""+escape(pair[1])+"\"")
		}
	}
	if len(parts) == 0 {
		return ""
	}
	return "{" + strings.Join(parts, ",") + "}"
}

func (x *Xray) Metrics() string {
	x.mu.Lock()
	defer x.mu.Unlock()
	families := map[string][]string{}
	types := map[string]string{}
	add := func(name, kind, labelText string, value any) {
		types[name] = kind
		families[name] = append(families[name], fmt.Sprintf("%s%s %v", name, labelText, value))
	}
	for key, value := range x.counters {
		base := key.labels
		text := renderLabels(base, "", "")
		if key.name == "rpc_xray_probe_requests_total" {
			result := base.outcome
			base.outcome = ""
			text = renderLabels(base, "result", result)
		}
		add(key.name, "counter", text, value)
	}
	for base, h := range x.histograms {
		name := "rpc_xray_request_duration_seconds"
		types[name] = "histogram"
		for i, bound := range contract.Buckets {
			families[name] = append(families[name], fmt.Sprintf("%s_bucket%s %d", name, renderLabels(base, "le", strconv.FormatFloat(bound, 'g', -1, 64)), h.buckets[i]))
		}
		families[name] = append(families[name], fmt.Sprintf("%s_bucket%s %d", name, renderLabels(base, "le", "+Inf"), h.count), fmt.Sprintf("%s_sum%s %v", name, renderLabels(base, "", ""), h.sum), fmt.Sprintf("%s_count%s %d", name, renderLabels(base, "", ""), h.count))
	}
	best := map[string]uint64{}
	for base, head := range x.heads {
		if head.block > best[base.chain] {
			best[base.chain] = head.block
		}
	}
	for base, head := range x.heads {
		text := renderLabels(base, "", "")
		add("rpc_xray_head_block", "gauge", text, head.block)
		add("rpc_xray_head_last_advance_timestamp_seconds", "gauge", text, float64(head.advanced.UnixNano())/1e9)
		add("rpc_xray_head_age_seconds", "gauge", text, math.Max(0, time.Since(head.advanced).Seconds()))
		add("rpc_xray_head_lag_blocks", "gauge", text, best[base.chain]-head.block)
	}
	for _, config := range []struct {
		name  string
		value *float64
	}{{"rpc_xray_value_per_request_usd", x.options.ValuePerRequest}, {"rpc_xray_revenue_per_hour_usd", x.options.RevenuePerHour}} {
		if config.value != nil && !math.IsNaN(*config.value) && !math.IsInf(*config.value, 0) && *config.value >= 0 {
			add(config.name, "gauge", "", *config.value)
		}
	}
	add("rpc_xray_offers_enabled", "gauge", "", 0)
	add("rpc_xray_info", "gauge", `{version="`+Version+`",lang="go"}`, 1)
	names := make([]string, 0, len(families))
	for name := range families {
		names = append(names, name)
	}
	sort.Strings(names)
	var out strings.Builder
	for _, name := range names {
		fmt.Fprintf(&out, "# HELP %s RPC X-Ray %s.\n# TYPE %s %s\n", name, name, name, types[name])
		sort.Strings(families[name])
		for _, line := range families[name] {
			out.WriteString(line + "\n")
		}
	}
	return out.String()
}
