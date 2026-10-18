-- Three-table migration scope: source trips.current_story_id values were all NULL at preflight.
-- The source also has fk_trips_current_story -> stories; this full-row test does not reproduce that FK.
CREATE TABLE yeodam_cdc_full309.users (
  user_id BIGINT NOT NULL AUTO_INCREMENT,
  email VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_as_cs NOT NULL,
  nickname VARCHAR(10) NOT NULL,
  profile_image_url VARCHAR(2048) DEFAULT NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  deleted_at DATETIME(6) DEFAULT NULL,
  PRIMARY KEY (user_id),
  UNIQUE KEY uk_users_active_email (email, (IF((deleted_at IS NULL),1,NULL)))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE yeodam_cdc_full309.trips (
  trip_id BIGINT NOT NULL AUTO_INCREMENT,
  user_id BIGINT NOT NULL,
  trip_name VARCHAR(10) NOT NULL,
  start_date DATE NOT NULL,
  end_date DATE NOT NULL,
  is_favorite TINYINT(1) NOT NULL DEFAULT 0,
  thumbnail_key VARCHAR(500) DEFAULT NULL,
  processing_status VARCHAR(20) NOT NULL DEFAULT 'PROCESSING',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  deleted_at DATETIME(6) DEFAULT NULL,
  current_story_id BIGINT DEFAULT NULL,
  PRIMARY KEY (trip_id),
  KEY IDX_TRIPS_LIST_START_DATE (user_id, deleted_at, start_date, trip_id),
  KEY IDX_TRIPS_FAVORITE_LIST_START_DATE (user_id, deleted_at, is_favorite, start_date, trip_id),
  KEY fk_trips_current_story (current_story_id),
  CONSTRAINT fk_trips_user FOREIGN KEY (user_id) REFERENCES yeodam_cdc_full309.users (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE yeodam_cdc_full309.trip_regions (
  region_id BIGINT NOT NULL AUTO_INCREMENT,
  trip_id BIGINT NOT NULL,
  region_code VARCHAR(5) NOT NULL,
  region_name VARCHAR(40) NOT NULL,
  latitude DECIMAL(10,8) NOT NULL,
  longitude DECIMAL(11,8) NOT NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  deleted_at DATETIME(6) DEFAULT NULL,
  PRIMARY KEY (region_id),
  KEY fk_trip_regions_trip (trip_id),
  CONSTRAINT fk_trip_regions_trip FOREIGN KEY (trip_id) REFERENCES yeodam_cdc_full309.trips (trip_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
