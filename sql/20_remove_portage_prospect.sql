PRAGMA foreign_keys = ON;

-- Operator already knows the Portage lab manager; remove from outbound pipeline.
DELETE FROM business_prospects
WHERE facility_id = 'MB-WPG-PORTAGE'
   OR facility LIKE 'Portage Regional%';
