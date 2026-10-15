.PHONY: install test lint format check recheck sandbox-image

# 安装依赖
install:
	pip install -e ".[dev]"

# 代码规范（与 CI 一致，提交前必跑：make check）
lint:
	ruff check src tests scripts

format:
	ruff format src tests scripts

# 跑全部测试
test:
	PYTHONPATH=src pytest tests/ -q

# 格式一致性检查（只查不改，与 CI 一致）
format-check:
	ruff format --check src tests scripts

# CI 入口：lint + format-check + test（+ 可选报告门禁）
check: lint format-check test
	@if [ -n "$(REPORT)" ] && [ -f "$(REPORT)" ]; then \
		echo "=== recheck ==="; \
		python scripts/recheck.py $(REPORT); \
	else \
		echo "跳过 recheck（未指定 REPORT）"; \
	fi

# 复跑判据门禁：需要传入 report.json
# 用法: make recheck REPORT=<report.json 路径>
recheck:
	python scripts/recheck.py $(REPORT)

# 构建执行沙箱镜像（agent 的 bash 工具在容器内运行，宿主隔离）
sandbox-image:
	docker build -f docker/sandbox.Dockerfile -t secpt-sandbox .

# 二进制/crypto 沙箱镜像（pwntools + 密码学库；pwn/crypto profile 自动选用）
sandbox-pwn-image:
	docker build -f docker/sandbox-pwn.Dockerfile -t secpt-sandbox-pwn .
