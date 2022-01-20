-- count(*) counts NULL rows; count(col) does not
-- created_at is timestamptz
-- NB: latest row per user via row_number; ties broken arbitrarily

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
