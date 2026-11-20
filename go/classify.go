package rpcxray

import (
	"bytes"
	"encoding/json"
	"math/big"
	"strconv"
	"strings"
)

var contract = func() struct {
	Methods        []string
	Chains         map[string]string
	Buckets        []float64
	CallerCodes    []float64
	RatePatterns   []string
	CallerPatterns []string
} {
	var value struct {
		Methods        []string
		Chains         map[string]string
		Buckets        []float64
		CallerCodes    []float64
		RatePatterns   []string
		CallerPatterns []string
	}
	if err := json.Unmarshal([]byte(contractJSON), &value); err != nil {
		panic(err)
	}
	return value
}()

func ChainLabel(id uint64) string {
	key := strconv.FormatUint(id, 10)
	if name, ok := contract.Chains[key]; ok {
		return name
	}
	return "eip155:" + key
}

type Entry struct {
	Method  string `json:"method"`
	Outcome string `json:"outcome"`
}
type call struct {
	id     string
	method string
}
type request struct {
	batch bool
	calls []call
}

func idKey(value any) string {
	number, ok := value.(json.Number)
	if !ok {
		encoded, _ := json.Marshal(value)
		return string(encoded)
	}
	mantissa := number.String()
	var exponent big.Int
	if split := strings.IndexAny(mantissa, "eE"); split >= 0 {
		exponent.SetString(mantissa[split+1:], 10)
		mantissa = mantissa[:split]
	}
	sign := ""
	if strings.HasPrefix(mantissa, "-") {
		sign = "-"
		mantissa = mantissa[1:]
	}
	integer, fraction, _ := strings.Cut(mantissa, ".")
	digits := strings.TrimLeft(integer+fraction, "0")
	if digits == "" {
		return "0"
	}
	significand := strings.TrimRight(digits, "0")
	shift := len(digits) - len(significand) - len(fraction)
	exponent.Add(&exponent, big.NewInt(int64(shift)))
	if exponent.Sign() == 0 {
		return sign + significand
	}
	return sign + significand + "e" + exponent.String()
}

func decodeJSON(body []byte) (any, bool) {
	var payload any
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.UseNumber()
	if !json.Valid(body) || decoder.Decode(&payload) != nil {
		return nil, false
	}
	return payload, true
}

func parseRequest(body []byte) *request {
	payload, valid := decodeJSON(body)
	if !valid {
		return nil
	}
	items, batch := payload.([]any)
	if !batch {
		items = []any{payload}
	}
	if len(items) == 0 {
		return nil
	}
	r := &request{batch: batch}
	for _, item := range items {
		object, ok := item.(map[string]any)
		if !ok {
			return nil
		}
		method, ok := object["method"].(string)
		if !ok {
			return nil
		}
		known := false
		for _, name := range contract.Methods {
			if name == method {
				known = true
				break
			}
		}
		if !known {
			method = "other"
		}
		r.calls = append(r.calls, call{idKey(object["id"]), method})
	}
	return r
}

func rpcError(value any) string {
	object, _ := value.(map[string]any)
	numeric, hasCode := object["code"].(json.Number)
	code, _ := numeric.Float64()
	message, _ := object["message"].(string)
	message = strings.ToLower(message)
	if code == 429 {
		return "rate_limited"
	}
	for _, pattern := range contract.RatePatterns {
		if strings.Contains(message, pattern) {
			return "rate_limited"
		}
	}
	if hasCode {
		for _, caller := range contract.CallerCodes {
			if code == caller {
				return "caller_error"
			}
		}
	}
	for _, pattern := range contract.CallerPatterns {
		if strings.Contains(message, pattern) {
			return "caller_error"
		}
	}
	return "rpc_error"
}

func responseOutcome(value any) string {
	object, ok := value.(map[string]any)
	if !ok {
		return "rpc_error"
	}
	if errorValue := object["error"]; errorValue != nil {
		return rpcError(errorValue)
	}
	if _, ok := object["result"]; ok {
		return "ok"
	}
	return "rpc_error"
}

func classify(r *request, status int, body []byte, failure string) []Entry {
	if r == nil {
		return []Entry{}
	}
	all := func(outcome string) []Entry {
		entries := make([]Entry, len(r.calls))
		for i, c := range r.calls {
			entries[i] = Entry{c.method, outcome}
		}
		return entries
	}
	if failure != "" {
		return all(failure)
	}
	if status == 429 {
		return all("rate_limited")
	}
	if status >= 400 {
		return all("http_error")
	}
	if body == nil {
		return all("ok")
	}
	payload, valid := decodeJSON(body)
	if !valid {
		return all("rpc_error")
	}
	items, batch := payload.([]any)
	if !batch {
		if r.batch {
			object, _ := payload.(map[string]any)
			if object["error"] == nil || object["id"] != nil {
				return all("rpc_error")
			}
		}
		return all(responseOutcome(payload))
	}
	byID := map[string]string{}
	for _, item := range items {
		if object, ok := item.(map[string]any); ok {
			byID[idKey(object["id"])] = responseOutcome(object)
		}
	}
	entries := all("rpc_error")
	for i, c := range r.calls {
		if outcome, ok := byID[c.id]; ok {
			entries[i].Outcome = outcome
		}
	}
	return entries
}

func hexHead(value any) (uint64, bool) {
	text, ok := value.(string)
	if !ok || len(text) < 3 || len(text) > 18 || text[:2] != "0x" {
		return 0, false
	}
	head, err := strconv.ParseUint(text[2:], 16, 64)
	return head, err == nil
}

func headFrom(r *request, body []byte) (uint64, bool) {
	if r == nil || body == nil {
		return 0, false
	}
	ids := map[string]bool{}
	for _, c := range r.calls {
		if c.method == "eth_blockNumber" {
			ids[c.id] = true
		}
	}
	if len(ids) == 0 {
		return 0, false
	}
	payload, valid := decodeJSON(body)
	if !valid {
		return 0, false
	}
	items, batch := payload.([]any)
	if !batch {
		items = []any{payload}
	}
	var best uint64
	found := false
	for _, item := range items {
		object, ok := item.(map[string]any)
		if !ok || object["error"] != nil {
			continue
		}
		if object["id"] == nil || !ids[idKey(object["id"])] {
			continue
		}
		if head, ok := hexHead(object["result"]); ok && (!found || head > best) {
			best, found = head, true
		}
	}
	return best, found
}
