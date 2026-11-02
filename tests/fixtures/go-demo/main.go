// go-demo: GET /health -> {"ok":true} on :18213 (stdlib only).
package main

import (
	"encoding/json"
	"net/http"
)

func main() {
	http.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]bool{"ok": true})
	})
	_ = http.ListenAndServe("127.0.0.1:18213", nil)
}
