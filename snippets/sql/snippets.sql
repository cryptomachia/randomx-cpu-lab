-- Note: count(*) counts NULL rows; count(col) does not
-- add an index on (user_id, created_at) for the window query
-- GROUP BY 1 refers to the first select expression
-- NB: Postgres syntax; date_trunc differs on MySQL

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
