-- Distinguish a dead/erroring source from a merely quiet one.
--
-- `consecutive_empty_runs` (added in 001) counts runs that returned no NEW items
-- (304 or windowed-out) — which a healthy, quiet feed does too. This adds a
-- separate counter that increments only when a fetch actually FAILS, giving the
-- (M4) `status` command an honest health signal.

ALTER TABLE source_runs ADD COLUMN consecutive_error_runs INTEGER NOT NULL DEFAULT 0;
