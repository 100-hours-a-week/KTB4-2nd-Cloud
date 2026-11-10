CREATE INDEX idx_trips_unstarted_timeout
    ON trips (processing_status, deleted_at, created_at, trip_id);
