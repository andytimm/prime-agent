# Set up the Claude collaborator on a second Mac

Use the **same collaborator checkout**, not the stock Prime Agent 0.10.5 release. On the source Mac, record `git rev-parse HEAD` and check `git status --short`: a clone at that SHA does not include uncommitted changes. Transfer those changes too, including `local-skills/claude-code/`. Do not copy credentials, session logs, `.venv`, or `target/`.

On the second Mac, start at the checkout root in a native terminal. Have Xcode Command Line Tools, Rust/Cargo, Python 3.11+, `uv`, and the native `claude` CLI on PATH. Sign in to Claude Code with your subscription on this Mac; configure Prime's own model access separately.

## Build the complete package

Run these blocks in order in the same shell. Use a fresh output directory; this leaves any stock launcher untouched.

```sh
set -e
out="$HOME/.local/share/prime-agent/local-builds/collaborator-$(git rev-parse --short HEAD)"
mkdir -p "$(dirname "$out")"
mkdir "$out"
ref=$(python3 scripts/release/bundle_catalog.py resolve-ref)
printf '%s\n' "$ref" > "$out/catalog-ref"
python3 scripts/release/bundle_catalog.py generate --network --catalog-ref "$ref" --out "$out/catalog"
python3 scripts/package_release.py --catalog-assets "$out/catalog" --out-dir "$out"
```

To reproduce the same catalogs later, use the recorded `catalog-ref` instead of resolving it again. The packager builds `pa-cli`, stages the runtime, built-in skills and validated catalogs beside the executable, and writes a checksummed archive. Keep the staged directory intact; copying only `prime-agent` misses the runtime changes.

```sh
case "$(uname -m)" in arm64) tag=darwin-arm64 ;; x86_64) tag=darwin-x64 ;; *) exit 1 ;; esac
pkg="$out/prime-agent-0.10.5-$tag"
(cd "$out" && shasum -a 256 -c SHA256SUMS)
unset PI_PACKAGE_DIR
"$pkg/prime-agent" --version  # 0.10.5
```

## Add the local skill and opt into previews

The packager copies `skills/`, **not** `local-skills/`. Install the matching skill separately. If the skill directory already exists, back it up outside `skills/` first rather than mixing revisions.

```sh
agent_dir="${PRIME_AGENT_CODING_AGENT_DIR:-$HOME/.prime/agent}"
skill="$agent_dir/skills/claude-code"
mkdir -p "$agent_dir/skills"
mkdir "$skill"
rsync -a --exclude=.venv --exclude=__pycache__ local-skills/claude-code/ "$skill/"
uv run --locked --project "$skill" python -m unittest discover -s "$skill/tests"
touch "$agent_dir/claude-agents-view.enabled"
"$pkg/prime-agent"
```

In a **saved session** in a trusted checkout, ask Prime to have Claude Code review the README read-only using `shell=bash`. Open Agents view and select the Claude row to inspect its recent exchange. Use the skill, not the row, to send a follow-up. The marker enables bounded local copies of prompt/result text for new jobs; remove it to turn the default off, or pass `agents_view=False` for one job. To return to stock Prime, exit and use your original launcher.
