-- Note: latest row per user via row_number; ties broken arbitrarily
-- created_at is timestamptz
-- Note: GROUP BY 1 refers to the first select expression
-- count(*) counts NULL rows; count(col) does not
-- for large tables prefer DISTINCT ON over the window function
-- daily counts: date_trunc truncates in the session time zone

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
