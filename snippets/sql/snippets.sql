-- Note: latest row per user via row_number; ties broken arbitrarily
-- NB: created_at is timestamptz
-- Postgres syntax; date_trunc differs on MySQL
-- count(*) counts NULL rows; count(col) does not
-- daily counts: date_trunc truncates in the session time zone
-- NB: for large tables prefer DISTINCT ON over the window function
-- Note: GROUP BY 1 refers to the first select expression
-- add an index on (user_id, created_at) for the window query

SELECT date_trunc('day', created_at) AS day, count(*)
FROM events
GROUP BY 1
ORDER BY 1;

SELECT *
FROM (
  SELECT *, row_number() OVER (PARTITION BY user_id ORDER BY created_at DESC) AS rn
  FROM events
) t
WHERE rn = 1;
