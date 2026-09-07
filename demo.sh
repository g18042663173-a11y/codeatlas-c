#!/usr/bin/env bash
# 一键复现全部结果。不需要 API key / 不需要 GPU / 不需要下载模型。
#
# 用法：
#   bash demo.sh            主语料 lwIP（固定公开 commit）
#   bash demo.sh cjson      小语料 cJSON（固定公开 commit）
#   bash demo.sh portfolio  求职专用 cJSON 演示（含审核闸门与拒答）
#   bash demo.sh workflow   portfolio + Agent/会话知识卡 6 场景闭环评测
set -e
export PYTHONHASHSEED=1
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"

if [ -n "${CODEATLAS_PYTHON:-}" ] && [ -x "$CODEATLAS_PYTHON" ]; then
  # acceptance-eval passes its own interpreter so nested demo steps cannot
  # accidentally fall back to a stale local .venv (for example an iCloud
  # placeholder).  Normal users and CI still use the installed CLI below.
  CODEATLAS=("$CODEATLAS_PYTHON" -m codeatlas.cli)
elif command -v codeatlas >/dev/null 2>&1; then
  CODEATLAS=(codeatlas)
elif [ -x .venv/bin/codeatlas ]; then
  # Let a checked-in local virtual environment run the demo without requiring
  # manual activation. PYTHONPATH above also supports editable installs.
  CODEATLAS=(.venv/bin/codeatlas)
else
  echo "[ERROR] 未找到 codeatlas；请先执行 python -m pip install -e '.[dev]'。" >&2
  exit 127
fi

CORPUS=${1:-lwip}
PORTFOLIO=false
if [ "$CORPUS" = "portfolio" ] || [ "$CORPUS" = "workflow" ]; then
  [ "$CORPUS" = "workflow" ] && WORKFLOW=true || WORKFLOW=false
  CORPUS=cjson
  PORTFOLIO=true
else
  WORKFLOW=false
fi
CJSON_COMMIT=fb16e5cf358798aabb049655975cde8427101056
LWIP_COMMIT=3d896ba0a37ff3ce73270ca5e230707fe47f60e3

clone_at_commit() {
  local url=$1 dir=$2 commit=$3
  if [ ! -d "$dir/.git" ]; then
    git init -q "$dir"
    git -C "$dir" remote add origin "$url"
    git -C "$dir" fetch -q --depth 1 origin "$commit"
    git -C "$dir" checkout -q --detach FETCH_HEAD
  fi
  local actual
  actual=$(git -C "$dir" rev-parse HEAD)
  if [ "$actual" != "$commit" ]; then
    echo "[ERROR] $dir 当前为 $actual，期望固定语料提交 $commit" >&2
    echo "请移走该目录后重新运行；脚本不会覆盖已有语料。" >&2
    exit 2
  fi
}

setup_lwip() {
  clone_at_commit https://github.com/lwip-tcpip/lwip corpus/lwip "$LWIP_COMMIT"
  # lwIP 没有现成的 compile_commands.json，用它自带的 unix port 配置生成一份
  python3 - <<'PY'
import json, os, glob
root = os.path.abspath("corpus/lwip")
inc = [f"-I{root}/src/include",
       f"-I{root}/contrib/ports/unix/port/include",
       f"-I{root}/test/unit"]
srcs = sorted(glob.glob(f"{root}/src/**/*.c", recursive=True))
json.dump([{"directory": root, "file": s,
            "arguments": ["cc", *inc, "-std=c99", "-DLWIP_DEBUG", "-c", s, "-o", s[:-2]+".o"]}
           for s in srcs], open(f"{root}/compile_commands.json", "w"))
print(f"  生成 compile_commands.json：{len(srcs)} 个翻译单元")
PY
  REPO=corpus/lwip; DB=data/lwip.db; DATA=data/lw; SYMBOL=netif_add
  QUESTIONS=eval/questions_lwip.yaml
  REPORT=docs/EVAL-LWIP.md
  TASKS=eval/tasks_lwip.yaml
  TASK_REPORT=docs/TASK-EVAL-LWIP.md
  WIKI_OUT=kb/wiki-lwip
  PARSE_ARGS="--compile-db corpus/lwip"
}

setup_cjson() {
  clone_at_commit https://github.com/DaveGamble/cJSON corpus/cJSON "$CJSON_COMMIT"
  # cJSON's CMake project can generate this file, but the portfolio demo should
  # also run on minimal CI/macOS hosts without CMake.  Generate the equivalent
  # compile database directly from the pinned public sources and public tests.
  python3 - <<'PY'
import glob, json, os
root = os.path.abspath("corpus/cJSON")
srcs = [
    os.path.join(root, name)
    for name in ("cJSON.c", "cJSON_Utils.c", "test.c")
]
srcs += sorted(glob.glob(os.path.join(root, "fuzzing", "*.c")))
srcs += sorted(glob.glob(os.path.join(root, "tests", "*.c")))
inc = [f"-I{root}", f"-I{root}/tests", f"-I{root}/tests/unity/src",
       f"-I{root}/tests/unity/examples"]
commands = [
    {"directory": root, "file": source,
     "arguments": ["cc", *inc, "-std=c89", "-c", source,
                   "-o", source[:-2] + ".o"]}
    for source in srcs
]
with open(os.path.join(root, "compile_commands.json"), "w", encoding="utf-8") as handle:
    json.dump(commands, handle, indent=2)
print(f"  生成 compile_commands.json：{len(srcs)} 个翻译单元")
PY
  REPO=corpus/cJSON; DB=data/kb.db; DATA=data; SYMBOL=cJSON_Delete
  QUESTIONS=eval/questions.yaml
  REPORT=docs/EVAL-CJSON.md
  TASKS=eval/tasks_cjson.yaml
  TASK_REPORT=docs/TASK-EVAL-CJSON.md
  WIKI_OUT=kb/wiki-cjson
  PARSE_ARGS="--compile-db corpus/cJSON"
}

[ "$CORPUS" = "cjson" ] && setup_cjson || setup_lwip
TOTAL_STEPS=8
[ "$PORTFOLIO" = true ] && TOTAL_STEPS=9
[ "$WORKFLOW" = true ] && TOTAL_STEPS=10

echo "▶ 1-5/$TOTAL_STEPS 冻结源码、解析、Wiki、索引、完整性验证并切换快照"
"${CODEATLAS[@]}" snapshot build "$REPO" "$REPO/compile_commands.json" --db "$DB" --activate
echo "▶ 6/$TOTAL_STEPS 影响分析"; "${CODEATLAS[@]}" impact $SYMBOL --db $DB --depth 3
echo "▶ 7/$TOTAL_STEPS 消融实验"; "${CODEATLAS[@]}" eval --db $DB --questions $QUESTIONS --data-dir $DATA --out $REPORT
echo "▶ 8/$TOTAL_STEPS 人工审核任务评测"
"${CODEATLAS[@]}" task-eval --db $DB --tasks "$TASKS" --data-dir $DATA \
  --out "$TASK_REPORT" --require-approved
if [ "$PORTFOLIO" = true ]; then
  echo "▶ 9/$TOTAL_STEPS 求职专用演示（隔离数据库）"
  WORKFLOW_ARGS=()
  [ "$WORKFLOW" = true ] && WORKFLOW_ARGS=(--workflow)
  "${CODEATLAS[@]}" portfolio-demo --db data/kb.db --work-dir data/portfolio-demo "${WORKFLOW_ARGS[@]}"
fi
if [ "$WORKFLOW" = true ]; then
  echo "▶ 10/$TOTAL_STEPS Agent / 会话知识卡闭环评测（隔离数据库）"
  "${CODEATLAS[@]}" workflow-eval --db data/kb.db \
    --work-dir data/portfolio-demo/workflow-eval \
    --out docs/WORKFLOW-EVAL-CJSON.md
fi

echo
echo "完成。评测报告：$REPORT"
echo "任务评测报告：$TASK_REPORT"
[ "$PORTFOLIO" = true ] && echo "求职演示留档：data/portfolio-demo/PORTFOLIO-DEMO.md"
[ "$WORKFLOW" = true ] && echo "闭环评测留档：docs/WORKFLOW-EVAL-CJSON.md"
echo "启动界面：codeatlas serve --db $DB --data-dir $DATA"
echo "运行测试：pytest -q"
