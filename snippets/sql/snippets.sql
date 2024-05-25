-- NB: created_at is timestamptz
-- Postgres syntax; date_trunc differs on MySQL
-- NB: add an index on (user_id, created_at) for the window query
-- NB: daily counts: date_trunc truncates in the session time zone
-- count(*) counts NULL rows; count(col) does not
-- Note: GROUP BY 1 refers to the first select expression
-- Note: for large tables prefer DISTINCT ON over the window function

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
