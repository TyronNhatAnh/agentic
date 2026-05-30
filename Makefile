.PHONY: help install venv run debug restart stop status logs test lint clean db-show db-stats db-reset pidfile

PYTHON      ?= python3
VENV        ?= .venv
VENV_BIN    := $(VENV)/bin
PIP         := $(VENV_BIN)/pip
PY          := $(VENV_BIN)/python
PKG         := agentic
PID_FILE    := .agentic.pid
LOG_FILE    := agentic.log
DB          ?= agentic.db

help:
	@echo "agentic — make targets"
	@echo ""
	@echo "  make install     create venv and install package (editable, with dev extras)"
	@echo "  make run         run in foreground (Ctrl-C to stop)"
	@echo "  make debug       run in foreground with LOG_LEVEL=DEBUG"
	@echo "  make start       run in background, pid -> $(PID_FILE), logs -> $(LOG_FILE)"
	@echo "  make stop        stop background process (if any)"
	@echo "  make restart     stop + start"
	@echo "  make status      show background process status"
	@echo "  make logs        tail $(LOG_FILE)"
	@echo "  make test        run pytest"
	@echo "  make db-show     show last 20 rows of runs table"
	@echo "  make db-stats    cache_read ratio / cost-per-thread / tool fail rate"
	@echo "  make db-reset    delete local SQLite db ($(DB))"
	@echo "  make clean       remove caches and __pycache__"

$(VENV)/bin/activate:
	$(PYTHON) -m venv $(VENV)

venv: $(VENV)/bin/activate

install: venv
	$(PIP) install -U pip
	$(PIP) install -e ".[dev]"
	@if [ ! -f .env ]; then cp .env.example .env && echo "→ created .env from .env.example (fill in tokens)"; fi

run:
	$(PY) -m $(PKG).main

debug:
	LOG_LEVEL=DEBUG $(PY) -m $(PKG).main

start:
	@if [ -f $(PID_FILE) ] && kill -0 `cat $(PID_FILE)` 2>/dev/null; then \
		echo "already running (pid=`cat $(PID_FILE)`)"; exit 1; \
	fi
	@nohup $(PY) -m $(PKG).main < /dev/null >> $(LOG_FILE) 2>&1 & echo $$! > $(PID_FILE)
	@sleep 1
	@echo "started (pid=`cat $(PID_FILE)`), logs -> $(LOG_FILE)"

stop:
	@if [ -f $(PID_FILE) ]; then \
		PID=`cat $(PID_FILE)`; \
		if kill -0 $$PID 2>/dev/null; then kill $$PID && echo "stopped pid=$$PID"; \
		else echo "stale pidfile (pid=$$PID not running)"; fi; \
		rm -f $(PID_FILE); \
	else echo "no pidfile, not running"; fi

restart: stop start

status:
	@if [ -f $(PID_FILE) ] && kill -0 `cat $(PID_FILE)` 2>/dev/null; then \
		echo "running (pid=`cat $(PID_FILE)`)"; \
	else echo "not running"; fi

logs:
	@touch $(LOG_FILE) && tail -f $(LOG_FILE)

test:
	$(VENV_BIN)/pytest -q

db-show:
	@sqlite3 $(DB) "select id, agent, status, duration_ms, substr(input,1,60) from runs order by id desc limit 20;"

db-stats:
	@echo "== brain cache_read ratio =="
	@sqlite3 $(DB) "select round(coalesce(sum(cache_read_input_tokens),0)*1.0 / nullif(sum(cache_read_input_tokens+cache_creation_input_tokens),0), 3) as cache_read_ratio from runs where agent='brain';"
	@echo "== cost per thread (top 10) =="
	@sqlite3 -header -column $(DB) "select thread_ts, round(sum(cost_usd),4) as cost_usd, sum(num_turns) as turns from runs where cost_usd is not null group by thread_ts order by cost_usd desc limit 10;"
	@echo "== tool fail rate =="
	@sqlite3 $(DB) "select round(sum(status='error')*1.0/count(*), 3) as fail_rate, count(*) as tool_calls from runs where agent like '%\_%' escape '\';"

db-reset:
	@rm -f $(DB) && echo "removed $(DB) (will be recreated on next start)"

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache build dist *.egg-info
