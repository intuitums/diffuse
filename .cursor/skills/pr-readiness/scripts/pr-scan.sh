#!/usr/bin/env bash
# pr-scan.sh — read-only pre-request scan.
#
# Prints branch scope facts and deterministic blocker candidates for the change
# between a base ref and the working tree. Makes no modification of any kind:
# no writes to the repository, no network, no config changes.
#
# Usage:  pr-scan.sh [base-ref]
# Exit:   0 when the scan completed (findings are reported, not signalled by exit
#         code); 1 only when the scan could not run at all.
#
# Dependencies: git, awk, grep, sed — nothing else. Portable across GNU and BSD
# userlands (Linux, macOS, WSL, CI containers).

set -uo pipefail

CAP=20   # max reported hits per category

say()     { printf '%s\n' "$*"; }
section() { printf '\n== %s ==\n' "$*"; }

# --- preflight --------------------------------------------------------------

if ! command -v git >/dev/null 2>&1; then
  say "pr-scan: git not found" >&2
  exit 1
fi

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  say "pr-scan: not a git repository" >&2
  exit 1
fi

TMP=$(mktemp -d 2>/dev/null) || { say "pr-scan: cannot create temp dir" >&2; exit 1; }
trap 'rm -rf "$TMP"' EXIT INT TERM

# --- scope ------------------------------------------------------------------

# symbolic-ref resolves the branch name even on an unborn branch, where
# rev-parse --abbrev-ref prints "HEAD" and exits non-zero.
BRANCH=$(git symbolic-ref --quiet --short HEAD 2>/dev/null) || BRANCH="(detached HEAD)"
[ -n "$BRANCH" ] || BRANCH="(unknown)"

UNBORN=no
git rev-parse --verify HEAD >/dev/null 2>&1 || UNBORN=yes
[ "$UNBORN" = "yes" ] && BRANCH="$BRANCH (unborn — no commits yet)"

# Base resolution ladder: argument, then remote default, then common trunks.
BASE=""
BASE_SRC=""
if [ "${1:-}" != "" ]; then
  BASE="$1"; BASE_SRC="argument"
else
  for remote in origin upstream; do
    ref=$(git symbolic-ref --short "refs/remotes/$remote/HEAD" 2>/dev/null) || continue
    if [ -n "$ref" ]; then BASE="$ref"; BASE_SRC="$remote/HEAD"; break; fi
  done
  if [ -z "$BASE" ]; then
    for cand in origin/main origin/master origin/develop origin/trunk \
                upstream/main upstream/master main master develop trunk; do
      if git rev-parse --verify --quiet "$cand" >/dev/null 2>&1; then
        BASE="$cand"; BASE_SRC="fallback"; break
      fi
    done
  fi
fi

if [ -n "$BASE" ] && ! git rev-parse --verify --quiet "$BASE" >/dev/null 2>&1; then
  say "pr-scan: base ref '$BASE' does not exist" >&2
  BASE=""
fi

MB=""
if [ -n "$BASE" ] && [ "$UNBORN" = "no" ]; then
  MB=$(git merge-base HEAD "$BASE" 2>/dev/null || echo "")
fi

section "Scope"
say "vcs:      git"
say "branch:   $BRANCH"
say "base:     ${BASE:-unresolved} (${BASE_SRC:-none})"
say "mergebase: ${MB:-n/a}"

# Base freshness. This script never fetches, so the base ref may lag its remote.
# A stale base silently inflates the diff with work that is already merged, and
# "behind" cannot reveal it because that count uses the same stale ref.
if [ -n "$BASE" ]; then
  base_date=$(git log -1 --format=%cd --date=short "$BASE" 2>/dev/null || echo unknown)
  base_ts=$(git log -1 --format=%ct "$BASE" 2>/dev/null || echo 0)
  now_ts=$(date +%s 2>/dev/null || echo 0)
  if [ "${base_ts:-0}" -gt 0 ] && [ "${now_ts:-0}" -gt 0 ]; then
    base_age=$(( (now_ts - base_ts) / 86400 ))
    say "base tip: $base_date (${base_age}d old) — freshness UNVERIFIED, this scan does not fetch"
  else
    say "base tip: $base_date — freshness UNVERIFIED, this scan does not fetch"
  fi
fi

if [ -n "$MB" ]; then
  say "head:     $(git rev-parse --short HEAD)"
  ncommits=$(git rev-list --count "$MB..HEAD" 2>/dev/null || echo 0)
  say "commits:  $ncommits"
  say "behind:   $(git rev-list --count "$MB..$BASE" 2>/dev/null || echo 0) (measured against the same possibly-stale base)"
  git diff --shortstat "$MB..HEAD" 2>/dev/null | sed 's/^/stat:    /'
  if [ "$ncommits" = "0" ]; then
    say "note:     branch has no commits ahead of base"
  fi

  # Scope sanity. A range that looks like it holds other people's merged work is
  # far more likely to mean a stale base than a genuinely huge branch.
  nauthors=$(git log --format='%ae' "$MB..HEAD" 2>/dev/null | sort -u | wc -l | tr -d ' ')
  nmerged=$(git log --format='%s' "$MB..HEAD" 2>/dev/null | grep -cE '\(#[0-9]+\)[[:space:]]*$' 2>/dev/null || true)
  nauthors=${nauthors:-0}; nmerged=${nmerged:-0}
  if [ "$ncommits" -gt 15 ] || [ "$nauthors" -gt 2 ] || [ "$nmerged" -ge 3 ]; then
    say ""
    say "WARNING:  this range does not look like one branch — $ncommits commits, $nauthors authors,"
    say "          $nmerged subjects ending in (#NNN), which usually means already-merged work."
    say "          Suspect a stale base. Fetch, re-resolve the base, and re-run before trusting"
    say "          any number above or any finding below."
  fi
else
  say "note:     no merge base; reporting working-tree changes only"
fi

DIRTY=$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')
say "dirty:    $DIRTY path(s) uncommitted or untracked"

# --- build the review corpus ------------------------------------------------

# Changed, still-present files (committed range + staged + unstaged), NUL-safe.
{
  [ -n "$MB" ] && git diff --name-only --diff-filter=d -z "$MB..HEAD"
  git diff --name-only --diff-filter=d -z
  git diff --name-only --diff-filter=d -z --cached
  git ls-files --others --exclude-standard -z
} > "$TMP/files" 2>/dev/null
tr '\0' '\n' < "$TMP/files" | grep -v '^$' | sort -u > "$TMP/files.txt"

# Added paths only (for artifact detection).
: > "$TMP/added.txt"
if [ -n "$MB" ]; then
  git diff --name-only --diff-filter=A "$MB..HEAD" >> "$TMP/added.txt" 2>/dev/null
fi
git ls-files --others --exclude-standard >> "$TMP/added.txt" 2>/dev/null
sort -u -o "$TMP/added.txt" "$TMP/added.txt"

# Added lines as "path:line:content", so findings cite real locations and
# pre-existing code is never attributed to this change.
added_lines() {
  awk '
    /^\+\+\+ /   { p = substr($0, 5); sub(/^b\//, "", p); file = p; next }
    /^@@ /       { s = $0; sub(/^.*\+/, "", s); sub(/[, ].*$/, "", s); ln = s + 0; next }
    /^\\/        { next }
    /^\+/        { if (file != "" && file != "/dev/null")
                     printf "%s:%d:%s\n", file, ln, substr($0, 2); ln++; next }
    /^-/         { next }
                 { ln++ }
  '
}

{
  [ -n "$MB" ] && git diff "$MB..HEAD" 2>/dev/null
  git diff 2>/dev/null
  git diff --cached 2>/dev/null
} | added_lines > "$TMP/addedlines.txt"

# Untracked files count as fully added. Skip binaries: scanning them yields
# noise, not findings (grep -I reports no text match for a binary file).
while IFS= read -r f; do
  [ -f "$f" ] || continue
  git check-ignore -q -- "$f" 2>/dev/null && continue
  grep -Iq . "$f" 2>/dev/null || continue
  awk -v F="$f" 'NR<=5000 { printf "%s:%d:%s\n", F, NR, $0 }' "$f" 2>/dev/null
done < <(git ls-files --others --exclude-standard 2>/dev/null) >> "$TMP/addedlines.txt"

TOTAL_ADDED=$(wc -l < "$TMP/addedlines.txt" | tr -d ' ')
FILE_COUNT=$(wc -l < "$TMP/files.txt" | tr -d ' ')
say "corpus:   $FILE_COUNT changed file(s), $TOTAL_ADDED added line(s) scanned"

# Secret rules read a filtered corpus: scanner configs and example/sample files
# exist to contain credential-shaped strings, so matching them is noise.
grep -vE '^([^:]*/)?(\.gitleaks\.toml|\.secrets\.baseline|\.trufflehogignore|[^:]*\.(example|sample|template|dist)):' \
  "$TMP/addedlines.txt" > "$TMP/addedlines.secrets.txt" 2>/dev/null || \
  cp "$TMP/addedlines.txt" "$TMP/addedlines.secrets.txt"

# report NAME PATTERN [redact] — increments HITS when anything is found, so a
# section can honestly print "none" rather than staying silent.
HITS=0
report() {
  name=$1; pat=$2; redact=${3:-no}
  src="$TMP/addedlines.txt"
  [ "$redact" = "redact" ] && src="$TMP/addedlines.secrets.txt"
  grep -nE "$pat" "$src" 2>/dev/null | cut -d: -f2- > "$TMP/hits" || true
  n=$(wc -l < "$TMP/hits" | tr -d ' ')
  [ "$n" = "0" ] && return 0
  HITS=$((HITS + 1))
  printf '%s: %s hit(s)\n' "$name" "$n"
  # Split off exactly two leading fields (path, line); the content keeps its own
  # colons, which field-splitting would destroy.
  awk -v cap="$CAP" -v redact="$redact" 'NR<=cap {
    i = index($0, ":");        if (i == 0) next
    path = substr($0, 1, i-1); rest = substr($0, i+1)
    j = index(rest, ":");      if (j == 0) next
    lineno  = substr(rest, 1, j-1)
    content = substr(rest, j+1)
    if (redact == "redact") { printf "  %s:%s  [redacted]\n", path, lineno; next }
    gsub(/^[ \t]+/, "", content)
    if (length(content) > 120) content = substr(content, 1, 120) "..."
    printf "  %s:%s  %s\n", path, lineno, content
  }' "$TMP/hits"
  [ "$n" -gt "$CAP" ] && printf '  ... %s more\n' "$((n - CAP))"
  return 0
}

# --- scans ------------------------------------------------------------------

section "Merge conflicts"
CONF=0
if [ -s "$TMP/files.txt" ]; then
  while IFS= read -r f; do
    [ -f "$f" ] || continue
    if grep -nE '^(<{7}|={7}|>{7})( |$)' -- "$f" >/dev/null 2>&1; then
      grep -nE '^(<{7}|={7}|>{7})( |$)' -- "$f" 2>/dev/null |
        awk -F: -v F="$f" '{ printf "  %s:%s  conflict marker\n", F, $1 }'
      CONF=1
    fi
  done < "$TMP/files.txt"
fi
if git ls-files -u 2>/dev/null | grep -q .; then
  say "  index has unmerged entries (git ls-files -u)"
  CONF=1
fi
for st in MERGE_HEAD REBASE_HEAD CHERRY_PICK_HEAD REVERT_HEAD; do
  [ -e "$(git rev-parse --git-dir)/$st" ] && { say "  in-progress operation: $st"; CONF=1; }
done
[ "$CONF" = "0" ] && say "none"

section "Suspect added paths"
ART=0
while IFS= read -r f; do
  [ -n "$f" ] || continue
  named=0
  case "$f" in
    */node_modules/*|node_modules/*|*/dist/*|dist/*|*/build/*|build/*|*/out/*|out/*|\
    .next/*|*/.next/*|target/*|*/target/*|_build/*|*/_build/*|*/__pycache__/*|__pycache__/*|\
    .venv/*|*/.venv/*|vendor/*|*/vendor/*|coverage/*|*/coverage/*|.terraform/*|*/.terraform/*|\
    DerivedData/*|Pods/*|.gradle/*)
      say "  $f  build/dependency output"; ART=1; named=1 ;;
    .env|.env.*|*/.env|*/.env.*)
      case "$f" in *.example|*.sample|*.template|*.dist) ;; *)
        say "  $f  environment file"; ART=1; named=1 ;; esac ;;
    *.pem|*.key|*.p12|*.pfx|*.keystore|*/id_rsa|id_rsa|*.netrc|*/kubeconfig|*.tfstate|*.tfstate.*)
      say "  $f  credential/state file"; ART=1; named=1 ;;
    *.log|*.orig|*.rej|*.bak|*.swp|*.tmp|.DS_Store|*/.DS_Store|Thumbs.db)
      say "  $f  debris"; ART=1; named=1 ;;
    *.pyc|*.class|*.o|*.so|*.dylib|*.dll|*.exe)
      say "  $f  compiled artifact"; ART=1; named=1 ;;
  esac
  # Would the ignore rules have caught it? --no-index also catches files that
  # were force-added past an ignore rule. Skip when already named above, so a
  # single file is not reported twice.
  if [ "$named" = "0" ] && git check-ignore -q --no-index -- "$f" 2>/dev/null; then
    say "  $f  matches an ignore rule but is present in the change"; ART=1
  fi
done < "$TMP/added.txt"
# Large added blobs.
if [ -n "$MB" ]; then
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    sha=$(git rev-parse --quiet --verify "HEAD:$f" 2>/dev/null) || continue
    sz=$(git cat-file -s "$sha" 2>/dev/null) || continue
    if [ "${sz:-0}" -gt 1048576 ]; then
      say "  $f  large file ($((sz / 1024)) KB)"; ART=1
    fi
  done < "$TMP/added.txt"
fi
[ "$ART" = "0" ] && say "none"

section "Debug and temporary code"
HITS=0
report "console/debugger" '(console\.(log|debug|dir|trace)|debugger;)'
report "print-style debug" '(pdb\.set_trace|breakpoint\(\)|binding\.pry|byebug|dbg!\(|spew\.Dump|printStackTrace|var_dump\(|NSLog\(|Debug\.WriteLine)'
report "focused/skipped tests" '(\.only\(|fdescribe\(|fit\(|xit\(|xdescribe\(|@pytest\.mark\.(skip|xfail)|t\.Skip\(|#\[ignore\]|@Ignore|@Disabled)'
report "new suppressions" '(eslint-disable|# noqa|# type: ignore|@ts-ignore|@ts-expect-error|nolint|NOSONAR|checkov:skip|tflint-ignore)'
report "local endpoints" '(localhost:[0-9]+|127\.0\.0\.1|0\.0\.0\.0:[0-9]+)'
[ "$HITS" = "0" ] && say "none"

section "Placeholders and unfinished work"
HITS=0
report "markers" '(^|[^A-Za-z0-9_])(TODO|FIXME|XXX|HACK|TBD)([^A-Za-z0-9_]|$)'
report "placeholder values" '(REPLACE_ME|CHANGEME|YOUR_[A-Z_]+_HERE|lorem ipsum|foo@example\.com)'
report "unimplemented" '(NotImplementedError|unimplemented!\(|todo!\(|panic!\("todo)'
[ "$HITS" = "0" ] && say "none"

section "Secret candidates (locations only, values redacted)"
HITS=0
report "aws-key"      '(AKIA|ASIA)[0-9A-Z]{16}' redact
report "github-token" '(gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,})' redact
report "gitlab-token" 'glpat-[A-Za-z0-9_-]{20,}' redact
report "llm-key"      '(sk-ant-[A-Za-z0-9_-]{20,}|sk-(proj-)?[A-Za-z0-9]{32,})' redact
report "slack-token"  'xox[baprs]-[A-Za-z0-9-]{10,}' redact
report "google-key"   'AIza[0-9A-Za-z_-]{35}' redact
report "stripe-live"  '(sk|rk)_live_[A-Za-z0-9]{10,}' redact
report "private-key"  '-----BEGIN [A-Z ]*PRIVATE KEY-----' redact
report "jwt"          'eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.' redact
# Require a quoted literal, or a bare value that ends the line. Without this,
# `token = github_token()` matches — an identifier is 12+ word characters too.
report "assignment"   '(api[_-]?key|secret|passwd|password|token)[[:space:]]*[:=][[:space:]]*(["'"'"'][A-Za-z0-9/+=_-]{12,}["'"'"']|[A-Za-z0-9/+=_-]{12,}[[:space:]]*$)' redact
report "conn-string"  '(postgres(ql)?|mysql|mongodb(\+srv)?|redis|amqp)://[^:@[:space:]]+:[^@[:space:]]+@' redact
[ "$HITS" = "0" ] && say "none"
if command -v gitleaks >/dev/null 2>&1 && [ -n "$MB" ]; then
  say "gitleaks: available — run 'gitleaks detect --no-banner --redact --log-opts \"$MB..HEAD\"' for an authoritative scan"
fi

section "Commit hygiene"
if [ -n "$MB" ]; then
  git log --format='%h %s' "$MB..HEAD" 2>/dev/null |
    grep -iE '(^[0-9a-f]+ (wip|temp|tmp|fixup!|squash!|asdf|test)([[:space:]]|$)|^[0-9a-f]+ [^ ]+$)' |
    head -n "$CAP" | sed 's/^/  /' || true
  authors=$(git log --format='%an <%ae>' "$MB..HEAD" 2>/dev/null | sort -u | wc -l | tr -d ' ')
  say "  distinct authors: $authors"
  say "  merge commits: $(git log --merges --oneline "$MB..HEAD" 2>/dev/null | wc -l | tr -d ' ')"
else
  say "  n/a (no merge base)"
fi

section "Uncommitted work"
if [ "$DIRTY" = "0" ]; then
  say "clean"
else
  git status --porcelain 2>/dev/null | head -n 40 | sed 's/^/  /'
fi

section "Notes"
say "This scan is advisory and pattern-based. Every hit needs review in context;"
say "absence of hits is not proof of correctness. No validation commands were run."
exit 0
