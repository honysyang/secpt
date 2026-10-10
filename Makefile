.PHONY: test recheck check install

# 安装依赖
install:
	pip install -e ".[dev]"

# 跑全部测试
test:
	PYTHONPATH=src pytest tests/ -q

# 复跑判据门禁：需要传入 report.json
# 用法: make recheck REPORT=data/reports/run.xxx.json
recheck:
	python scripts/recheck.py $(REPORT)

# CI 入口：测试 + 门禁（如果有报告文件）
check: test
	@if [ -n "$(REPORT)" ] && [ -f "$(REPORT)" ]; then \
		echo "=== recheck ==="; \
		python scripts/recheck.py $(REPORT); \
	else \
		echo "跳过 recheck（未指定 REPORT）"; \
	fi
