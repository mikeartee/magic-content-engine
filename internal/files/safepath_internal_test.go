package files

import (
	"errors"
	"math/rand"
	"path/filepath"
	"strings"
	"testing"
)

// confined reports whether resolved lies inside base (or equals it). It mirrors
// the invariant the guard must enforce: a resolved path never escapes the run
// directory.
func confined(t *testing.T, base, resolved string) bool {
	t.Helper()
	rel, err := filepath.Rel(base, resolved)
	if err != nil {
		return false
	}
	if rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return false
	}
	return true
}

// Requirement 4.1: a thorough table of traversal attempts must all be rejected
// with ErrForbidden — none may resolve inside the run directory.
func TestSafePathRejectsKnownTraversals(t *testing.T) {
	svc := New(filepath.Join(t.TempDir(), "output"))
	base := filepath.Join(svc.outputDir, "run1")

	attempts := []string{
		"..",
		"../",
		"../escape.md",
		"../../etc/passwd",
		"../../../../../../etc/shadow",
		"subdir/../../escape",
		"a/b/c/../../../../escape",
		"..\\escape.md",
		"..\\..\\windows\\system32",
		"sub\\..\\..\\escape",
		"foo/../../bar",
	}

	for _, name := range attempts {
		got, err := svc.safePath("run1", name)
		if !errors.Is(err, ErrForbidden) {
			t.Errorf("safePath(%q) err = %v (resolved %q), want ErrForbidden", name, err, got)
		}
	}

	// Absolute paths must never resolve outside the run dir either: they are
	// either confined (treated as a relative component) or rejected.
	for _, name := range []string{`/etc/passwd`, `C:\Windows\System32\drivers\etc\hosts`} {
		got, err := svc.safePath("run1", name)
		if err == nil && !confined(t, base, got) {
			t.Errorf("safePath(%q) resolved to %q, which escapes %q", name, got, base)
		}
	}
}

// Requirement 4 property: for ANY name, safePath either rejects it with
// ErrForbidden or resolves to a path confined within output/<run_id>/. No name
// ever escapes the run directory.
func TestSafePathPropertyNeverEscapes(t *testing.T) {
	svc := New(filepath.Join(t.TempDir(), "output"))
	const runID = "run1"
	base := filepath.Join(svc.outputDir, runID)

	segments := []string{
		"..", ".", "a", "foo", "bar", "post.md", "sub", "",
		"..\\", "../", "x/y", "x\\y", "C:", "etc", "passwd",
		"....//", "%2e%2e", "..%2f", " ", "\t",
	}

	rng := rand.New(rand.NewSource(0xC0FFEE))
	for i := 0; i < 5000; i++ {
		n := rng.Intn(6) + 1
		parts := make([]string, n)
		for j := 0; j < n; j++ {
			parts[j] = segments[rng.Intn(len(segments))]
		}
		// Randomly join with forward or back slashes to mimic mixed clients.
		sep := "/"
		if rng.Intn(2) == 0 {
			sep = "\\"
		}
		name := strings.Join(parts, sep)

		resolved, err := svc.safePath(runID, name)
		if err != nil {
			if !errors.Is(err, ErrForbidden) {
				t.Fatalf("safePath(%q) unexpected error: %v", name, err)
			}
			continue // rejection satisfies the invariant
		}
		if !confined(t, base, resolved) {
			t.Fatalf("safePath(%q) resolved to %q, which escapes base %q", name, resolved, base)
		}
	}
}
