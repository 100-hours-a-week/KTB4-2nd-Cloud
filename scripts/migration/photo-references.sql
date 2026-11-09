WITH live AS (
 SELECT a.trip_attachment_id
 FROM yeodam.trip_attachments a
 JOIN yeodam.trips t ON t.trip_id=a.trip_id
 JOIN yeodam.users u ON u.user_id=t.user_id
 WHERE t.deleted_at IS NULL AND u.deleted_at IS NULL AND (
   a.deleted_at IS NULL OR EXISTS (
    SELECT 1 FROM yeodam.story_blocks b JOIN yeodam.stories s ON s.story_id=b.story_id
    WHERE b.trip_attachment_id=a.trip_attachment_id AND b.deleted_at IS NULL AND s.deleted_at IS NULL
   )
 )
), refs AS (
 SELECT f.object_key AS object_key,'file.original' AS origin,
   CASE WHEN f.deleted_at IS NOT NULL OR u.deleted_at IS NOT NULL THEN 'deleted'
        WHEN f.upload_status='READY' THEN 'required' ELSE 'unfinished-upload' END AS category
 FROM yeodam.files f JOIN yeodam.users u ON u.user_id=f.user_id
 UNION ALL
 SELECT f.object_key,'attachment.original',
   CASE WHEN f.deleted_at IS NULL AND f.upload_status='READY' THEN 'required' ELSE 'blocked-file-state' END
 FROM yeodam.trip_attachments a JOIN live l ON l.trip_attachment_id=a.trip_attachment_id
 JOIN yeodam.files f ON f.file_id=a.file_id
 UNION ALL
 SELECT a.analyze_storage_key,'attachment.analyze',IF(l.trip_attachment_id IS NULL,'deleted','required')
 FROM yeodam.trip_attachments a LEFT JOIN live l ON l.trip_attachment_id=a.trip_attachment_id
 UNION ALL
 SELECT a.preview_storage_key,'attachment.preview',IF(l.trip_attachment_id IS NULL,'deleted','required')
 FROM yeodam.trip_attachments a LEFT JOIN live l ON l.trip_attachment_id=a.trip_attachment_id
 UNION ALL
 SELECT a.display_storage_key,'attachment.display',IF(l.trip_attachment_id IS NULL,'deleted','required')
 FROM yeodam.trip_attachments a LEFT JOIN live l ON l.trip_attachment_id=a.trip_attachment_id
 UNION ALL
 SELECT t.thumbnail_key,'trip.thumbnail',IF(t.deleted_at IS NULL AND u.deleted_at IS NULL,'required','deleted')
 FROM yeodam.trips t JOIN yeodam.users u ON u.user_id=t.user_id
 UNION ALL
 SELECT p.thumbnail_key,'place.thumbnail',
   IF(p.deleted_at IS NULL AND t.deleted_at IS NULL AND u.deleted_at IS NULL,'required','deleted')
 FROM yeodam.trip_detail_places p JOIN yeodam.trips t ON t.trip_id=p.trip_id
 JOIN yeodam.users u ON u.user_id=t.user_id
 UNION ALL
 SELECT i.object_key,'initial-upload.original',IF(i.trip_attachment_id IS NULL,'unfinished-upload','historical-upload')
 FROM yeodam.initial_attachment_upload_items i
 UNION ALL
 SELECT i.object_key,'additional-upload.original',IF(i.trip_attachment_id IS NULL,'unfinished-upload','historical-upload')
 FROM yeodam.additional_attachment_upload_items i
 UNION ALL
 SELECT u.profile_image_url,'user.profile',IF(u.deleted_at IS NULL,'required','deleted') FROM yeodam.users u
)
SELECT JSON_OBJECT('key',COALESCE(object_key,''),'origin',origin,'category',category)
FROM refs WHERE (object_key IS NOT NULL AND object_key<>'')
 OR (category='required' AND origin IN ('file.original','attachment.original','attachment.analyze','attachment.preview'))
ORDER BY origin,object_key,category;
