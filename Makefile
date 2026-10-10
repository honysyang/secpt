.PHONY: install test lint format check recheck

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

# CI 入口：lint + 测试（+ 可选报告门禁）
check: lint test
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
