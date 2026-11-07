package contracts

import (
	"crypto/sha256"
	"embed"
	"encoding/hex"
	"encoding/json"
)

//go:embed scenarios/*.json evaluators/*.json benchmarks/*.json agents/*.json tools/*.json v2/*.json v3/*.json
var Assets embed.FS

type Reference struct {
	ID      string          `json:"id"`
	Version int             `json:"version"`
	SHA256  string          `json:"sha256"`
	Content json.RawMessage `json:"content"`
}

func Load(path string) Reference {
	b, err := Assets.ReadFile(path)
	if err != nil {
		panic(err)
	}
	var identity struct {
		ID      string `json:"id"`
		Version int    `json:"version"`
	}
	if err := json.Unmarshal(b, &identity); err != nil {
		panic(err)
	}
	sum := sha256.Sum256(b)
	return Reference{identity.ID, identity.Version, hex.EncodeToString(sum[:]), b}
}
