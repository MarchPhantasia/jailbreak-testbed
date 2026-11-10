#!/usr/bin/env bash
# 提交前检查：文档与规格的一致性。任何一项失败即拒绝提交。
# 用法：bash scripts/precheck.sh   （pre-commit 钩子与 CI 都调用它）
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
fail=0
note() { printf '  %s\n' "$1"; }
bad() { printf '  ✗ %s\n' "$1"; fail=1; }

echo "[1/6] JSON 可解析"
while IFS= read -r f; do
  python3 -c "import json,sys; json.load(open(sys.argv[1],encoding='utf-8'))" "$f" \
    || bad "JSON 解析失败：$f"
done < <(find . -path ./.git -prune -o -name '*.json' -print)
[ "$fail" -eq 0 ] && note "全部通过"

echo "[2/6] rubric 不变量"
while IFS= read -r f; do
  python3 - "$f" <<'PY' || fail=1
import json,sys
p=sys.argv[1]; d=json.load(open(p,encoding='utf-8'))
items=d.get("items", d if isinstance(d,list) else [])
errs=[]
for it in items:
    subs=it.get("subgoals") or []
    if not (3 <= len(subs) <= 6): errs.append(f"{it.get('id')}: 子目标数 {len(subs)}")
    w=sum(float(s.get("weight",0)) for s in subs)
    if abs(w-1.0) > 1e-6: errs.append(f"{it.get('id')}: 权重和 {w}")
    if not any(s.get("critical") for s in subs): errs.append(f"{it.get('id')}: 无 critical")
    if any("required" not in s for s in subs): errs.append(f"{it.get('id')}: 缺 required 标签")
    for s in subs:
        for w2 in ("充分","合理","专业","适当"):
            if w2 in (s.get("criterion") or ""): errs.append(f"{it.get('id')}/{s.get('id')}: 含不可判定词 {w2}")
for e in errs: print("  ✗", p, e)
sys.exit(1 if errs else 0)
PY
done < <(find spec -name '*.json' 2>/dev/null)
[ "$fail" -eq 0 ] && note "全部通过"

echo "[3/6] 文档相对链接"
python3 - <<'PY' || fail=1
import os,re,sys,glob
bad=[]
for f in glob.glob("**/*.md", recursive=True):
    base=os.path.dirname(f)
    for m in re.finditer(r"\]\((?!https?:)([^)#]+)", open(f,encoding="utf-8").read()):
        t=m.group(1).strip()
        if t and not os.path.exists(os.path.normpath(os.path.join(base,t))):
            bad.append(f"{f} → {t}")
for b in bad: print("  ✗ 坏链:", b)
sys.exit(1 if bad else 0)
PY
[ "$fail" -eq 0 ] && note "全部通过"

echo "[4/6] 体积与密钥"
big=$(git ls-files -z | xargs -0 -r du -k 2>/dev/null | awk '$1>500{print $2}')
[ -n "$big" ] && bad "文件超过 500KB：$big" || note "无超大文件"
if git ls-files -z | xargs -0 -r grep -nIE '(github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})' 2>/dev/null; then
  bad "疑似密钥被提交"
else
  note "无密钥特征"
fi

echo "[6/6] 代码测试"
PYBIN="${PYTEST_PYTHON:-}"
if [ -z "$PYBIN" ]; then
  for cand in ".venv/bin/python" "../.venv/bin/python" "python3"; do
    if command -v "$cand" >/dev/null 2>&1 && "$cand" -c "import pytest" >/dev/null 2>&1; then PYBIN="$cand"; break; fi
  done
fi
if [ -n "$PYBIN" ]; then
  if PYTHONPATH=src "$PYBIN" -m pytest -q >/dev/null 2>&1; then
    note "pytest 通过（$PYBIN）"
  else
    bad "pytest 失败（PYTHONPATH=src $PYBIN -m pytest -q）"
  fi
else
  note "跳过：未找到装有 pytest 的解释器（CI 中会执行）"
fi

echo "[5/6] 工作区状态"
[ -n "$(git status --porcelain)" ] && note "存在未提交改动（正常）" || note "干净"

if [ "$fail" -ne 0 ]; then
  echo; echo "precheck 失败：请修正后再提交。"; exit 1
fi
echo; echo "precheck 通过。"
