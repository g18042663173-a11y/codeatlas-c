#!/usr/bin/env bash
# 一键复现全部结果。不需要 API key / 不需要 GPU / 不需要下载模型。
#
# 用法：
#   bash demo.sh            主语料 lwIP（135k 行协议栈，约 1 分钟）
#   bash demo.sh cjson      小语料 cJSON（3.5k 行，约 15 秒，适合快速验证）
set -e
export PYTHONHASHSEED=1

CORPUS=${1:-lwip}
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
  PARSE_ARGS="--compile-db corpus/lwip"
}

setup_cjson() {
  clone_at_commit https://github.com/DaveGamble/cJSON corpus/cJSON "$CJSON_COMMIT"
  REPO=corpus/cJSON; DB=data/kb.db; DATA=data; SYMBOL=cJSON_Delete
  QUESTIONS=eval/questions.yaml
  REPORT=docs/EVAL-CJSON.md
  PARSE_ARGS="-I corpus/cJSON"
}

# 测试固件跑在 cJSON 上（小、秒级），无论主语料选哪个都拉一份，保证 pytest 不跳过
clone_at_commit https://github.com/DaveGamble/cJSON corpus/cJSON "$CJSON_COMMIT"

[ "$CORPUS" = "cjson" ] && setup_cjson || setup_lwip

echo "▶ 1/7 解析（全量）"
codeatlas parse $REPO $PARSE_ARGS --db $DB --force

echo "▶ 2/7 增量验证（无变更，应秒回）"
codeatlas parse $REPO $PARSE_ARGS --db $DB

echo "▶ 3/7 摘要头";   codeatlas summary --db $DB
echo "▶ 4/7 分层文档"; codeatlas wiki   --db $DB
echo "▶ 5/7 索引";     codeatlas index  --db $DB --data-dir $DATA --embedder tfidf
echo "▶ 6/7 影响分析"; codeatlas impact $SYMBOL --db $DB --depth 3
echo "▶ 7/7 消融实验"; codeatlas eval --db $DB --questions $QUESTIONS --data-dir $DATA --out $REPORT

echo
echo "完成。评测报告：$REPORT"
echo "启动界面：codeatlas serve --db $DB --data-dir $DATA"
echo "运行测试：pytest -q"
