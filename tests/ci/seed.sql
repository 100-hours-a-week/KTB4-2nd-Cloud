-- The MySQL container and this identity exist only for the Cloud PR smoke.
INSERT INTO users (user_id, email, nickname)
VALUES (900001, 'cloud-ci@yeodam.invalid', 'CI검증');
INSERT INTO user_stats (user_id) VALUES (900001);
INSERT INTO consents (user_id, is_agreed, agreed_at)
VALUES (900001, TRUE, CURRENT_TIMESTAMP(6));
INSERT INTO login_sessions (sid, user_id, refresh_token_hash, expires_at)
VALUES (
  '00000000-0000-4000-8000-000000000001', 900001,
  'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
  CURRENT_TIMESTAMP(6) + INTERVAL 1 DAY
);
