#!/bin/sh
# 僅在 Volume 第一次初始化時執行：在遊戲資料庫啟用 pg_stat_statements（SQL 次數與耗時統計，用來找慢查詢）。
# 既有的 Volume 不會執行這個檔案，請手動執行一次（見 README「效能監控」）。
set -e
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
	CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
EOSQL
