import unittest

from scripts.analyze_pub_telegram import annotate_message, classify_message, extract_locations


class PubTelegramRuleTests(unittest.TestCase):
    def test_risk_is_not_occurred(self):
        label, evidence = classify_message('[Risk of Flash Floods] Due to heavy rain, avoid Road A.')
        self.assertEqual(label, 'flood_risk_notice')
        self.assertTrue(evidence)
        self.assertEqual(extract_locations('Flash flood at Road A', label), [])

    def test_subsided_wins_over_generic_flood(self):
        text = 'Flash flood subsided at Example Road A. [00:01 hours]'
        label, _ = classify_message(text)
        self.assertEqual(label, 'flood_subsided')
        self.assertEqual(extract_locations(text,label)[0]['location_text'],'Example Road A')

    def test_relation_not_split_at_and(self):
        text = '[FLASH FLOOD OCCURRED]\nFlash flood at Example Avenue B (Between Sample Road C and Demo Drive D). Please avoid the area. Issued 0001 hours.'
        locations = extract_locations(text,'flood_occurred')
        self.assertEqual(len(locations),1)
        self.assertIn('between_and',locations[0]['road_pattern_labels'])
        self.assertNotIn('Issued',locations[0]['location_text'])
        start,end = locations[0]['span']
        self.assertEqual(text[start:end],locations[0]['location_text'])

    def test_instruction_parenthesis_not_location(self):
        text = 'Flash flood at Road A. Please avoid for one (1) hour. [00:01hrs]'
        location = extract_locations(text,'flood_occurred')[0]
        self.assertEqual(location['location_text'],'Road A')
        self.assertNotIn('parenthetical_place',location['road_pattern_labels'])

    def test_multiple_semicolon_entries_and_unknown(self):
        locations = extract_locations('Flash flood at Road A; Road B. Issued 0001 hours.','flood_occurred')
        self.assertEqual([item['location_text'] for item in locations],['Road A','Road B'])
        row = annotate_message({'text':'[FLASH FLOOD OCCURRED] No location supplied.'})
        self.assertEqual(row['location_status'],'unknown')
        self.assertEqual(classify_message('A channel announcement')[0],'other')

    def test_published_time_and_body_time_are_distinct(self):
        row = annotate_message({'published_at_sgt':'2026-01-02T00:01:00+08:00', 'text':'Flash flood at Example Road A. Issued 0001 hours.'})
        self.assertEqual(row['body_clock_evidence'][0]['clock'],'0001')
        self.assertEqual(row['published_at_sgt'],'2026-01-02T00:01:00+08:00')

    def test_no_generic_word_flood_classifier(self):
        self.assertEqual(classify_message('PUB flood resilience update')[0],'other')


if __name__ == '__main__':
    unittest.main()
