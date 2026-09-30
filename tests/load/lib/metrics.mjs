import { Counter, Rate, Trend } from 'k6/metrics';

export const businessFailures = new Counter('yeodam_business_failures');
export const photosUploaded = new Counter('yeodam_photos_uploaded');
export const tripCreateDuration = new Trend('yeodam_trip_create_duration', true);
export const uploadIntermediateDuration = new Trend('yeodam_upload_intermediate_duration', true);
export const uploadFinalDuration = new Trend('yeodam_upload_final_duration', true);
export const tripViewDuration = new Trend('yeodam_trip_view_duration', true);
export const successfulJourneys = new Rate('yeodam_successful_journeys');
