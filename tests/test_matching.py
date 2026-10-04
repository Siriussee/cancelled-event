#!/usr/bin/env python3
"""Tests for matching StubHub events against Ticketmaster cancelled events."""

from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from cancelled_event import matching as matcher


def stubhub_row(**overrides: str) -> dict[str, str]:
    row = {
        "city": "Austin",
        "country": "US",
        "page": "0",
        "eventId": "sh-1",
        "name": "Taylorville - A Tribute to Taylor Swift",
        "url": "https://www.stubhub.com/taylorville-austin-tickets-6-27-2026/event/160906070/",
        "venueName": "Stubb's Waller Creek Amphitheater",
        "venueId": "venue-1",
        "formattedVenueLocation": "Austin, TX, USA",
        "categoryId": "1",
        "dayOfWeek": "Sat",
        "formattedDateWithoutYear": "Jun 27",
        "formattedTime": "7:00 PM",
        "allowPublicPurchase": "True",
        "hasActiveListings": "False",
    }
    row.update(overrides)
    return row


def ticketmaster_row(**overrides: str) -> dict[str, str]:
    row = {
        "countryCode": "US",
        "page": "0",
        "id": "tm-internal-1",
        "tmId": "tm-1",
        "name": "Taylorville - A Tribute to Taylor Swift",
        "url": "https://www.ticketmaster.com/taylorville/event/tm-1",
        "statusCode": "cancelled",
        "localDate": "2026-06-27",
        "dateTime": "2026-06-28T00:00:00Z",
        "venueName": "Stubb's Waller Creek Amphitheater",
        "venueCity": "Austin",
        "venueStateCode": "TX",
        "venueStateName": "Texas",
        "venueCountryCode": "US",
        "venueCountryName": "United States Of America",
        "venueLatitude": "30.266",
        "venueLongitude": "-97.736",
        "attractionIds": "att-1",
        "attractionNames": "Taylorville - A Tribute to Taylor Swift",
        "attractionUrls": "",
        "segmentId": "music",
        "segmentName": "Music",
        "attractionSegmentIds": "music",
        "attractionSegmentNames": "Music",
    }
    row.update(overrides)
    return row


class CancelledEventIntersectionTests(unittest.TestCase):
    def test_parse_stubhub_event_date_from_url(self) -> None:
        row = stubhub_row(url="https://www.stubhub.com/example-tickets-12-3-2026/event/1/")

        self.assertEqual(matcher.stubhub_event_date(row), "2026-12-03")

    def test_high_confidence_match_requires_exact_date_location_and_strong_text(self) -> None:
        matches = matcher.find_high_confidence_matches([stubhub_row()], [ticketmaster_row()])

        self.assertEqual(len(matches), 1)
        match = matches[0]
        self.assertEqual(match["stubhub_eventId"], "sh-1")
        self.assertEqual(match["ticketmaster_event_id"], "tm-1")
        self.assertEqual(match["match_confidence"], "high")
        self.assertEqual(match["event_date"], "2026-06-27")
        self.assertEqual(match["venue_score"], "1.000")
        self.assertEqual(match["title_score"], "1.000")

    def test_does_not_match_same_title_on_different_date(self) -> None:
        matches = matcher.find_high_confidence_matches(
            [stubhub_row()],
            [ticketmaster_row(localDate="2026-06-28")],
        )

        self.assertEqual(matches, [])

    def test_does_not_match_same_title_at_different_venue(self) -> None:
        matches = matcher.find_high_confidence_matches(
            [stubhub_row()],
            [ticketmaster_row(venueName="Moody Center")],
        )

        self.assertEqual(matches, [])

    def test_does_not_match_same_title_in_different_us_state(self) -> None:
        matches = matcher.find_high_confidence_matches(
            [stubhub_row(formattedVenueLocation="Austin, TX, USA")],
            [ticketmaster_row(venueStateCode="CA")],
        )

        self.assertEqual(matches, [])

    def test_does_not_match_stubhub_rows_that_disallow_public_purchase(self) -> None:
        matches = matcher.find_high_confidence_matches(
            [stubhub_row(allowPublicPurchase="False")],
            [ticketmaster_row()],
        )

        self.assertEqual(matches, [])

    def test_cancelled_marker_and_accents_do_not_prevent_high_confidence_match(self) -> None:
        matches = matcher.find_high_confidence_matches(
            [
                stubhub_row(
                    eventId="sh-2",
                    name="Riles",
                    url="https://www.stubhub.com/riles-montreal-tickets-6-28-2026/event/159996606/",
                    venueName="MTELUS",
                    formattedVenueLocation="Montreal, Canada",
                    country="CA",
                )
            ],
            [
                ticketmaster_row(
                    tmId="tm-2",
                    name="CANCELLED: Rilès",
                    localDate="2026-06-28",
                    venueName="MTELUS",
                    venueCity="Montreal",
                    venueStateCode="QC",
                    venueCountryCode="CA",
                    countryCode="CA",
                    attractionNames="Rilès",
                )
            ],
        )

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["stubhub_eventId"], "sh-2")

    def test_attraction_match_is_not_enough_when_title_is_too_broad(self) -> None:
        matches = matcher.find_high_confidence_matches(
            [
                stubhub_row(
                    name="YG",
                    url="https://www.stubhub.com/yg-garden-city-tickets-7-11-2026/event/159612703/",
                    venueName="Revolution Concert House & Event Center",
                    formattedVenueLocation="Garden City, ID, USA",
                )
            ],
            [
                ticketmaster_row(
                    name="YG - The JUST RE'D UP Tour",
                    localDate="2026-07-11",
                    venueName="Revolution Concert House & Event Center",
                    venueCity="Garden City",
                    venueStateCode="ID",
                    attractionNames="YG",
                )
            ],
        )

        self.assertEqual(matches, [])

    def test_substantial_short_title_matches_ticketmaster_tour_suffix(self) -> None:
        matches = matcher.find_high_confidence_matches(
            [
                stubhub_row(
                    name="Chad Gray",
                    url="https://www.stubhub.com/chad-gray-los-angeles-tickets-10-4-2026/event/161331875/",
                    venueName="The Bellwether",
                    formattedVenueLocation="Los Angeles, CA, USA",
                )
            ],
            [
                ticketmaster_row(
                    name="Chad Gray: 30 Years of Madnesss",
                    localDate="2026-10-04",
                    venueName="The Bellwether",
                    venueCity="Los Angeles",
                    venueStateCode="CA",
                    attractionNames="",
                )
            ],
        )

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["title_score"], "1.000")

    def test_multi_artist_title_matches_ticketmaster_tour_suffix(self) -> None:
        matches = matcher.find_high_confidence_matches(
            [
                stubhub_row(
                    name="Buju Banton and Stephen Marley",
                    url="https://www.stubhub.com/buju-banton-terre-haute-tickets-7-5-2026/event/160896240/",
                    venueName="The Mill Terre Haute",
                    formattedVenueLocation="Terre Haute, IN, USA",
                )
            ],
            [
                ticketmaster_row(
                    name="Buju Banton & Stephen Marley: Roots and Rhymes Tour",
                    localDate="2026-07-05",
                    venueName="The Mill Terre Haute",
                    venueCity="Terre Haute",
                    venueStateCode="IN",
                    attractionNames="Buju Banton|Stephen Marley",
                )
            ],
        )

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["title_score"], "1.000")

    def test_deduplicates_stubhub_rows_by_event_id(self) -> None:
        matches = matcher.find_high_confidence_matches(
            [stubhub_row(page="0"), stubhub_row(page="1")],
            [ticketmaster_row()],
        )

        self.assertEqual(len(matches), 1)

    def test_venue_country_overrides_cross_border_query_country(self):
        matches = matcher.find_high_confidence_matches(
            [stubhub_row(country="CA", formattedVenueLocation="Austin, TX, USA")],
            [ticketmaster_row()],
        )
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["country"], "US")

    def test_canceled_spelling_and_missing_tm_id_fallback_are_supported(self):
        row = ticketmaster_row(tmId="", statusCode="canceled")
        matches = matcher.find_high_confidence_matches([stubhub_row()], [row, row])
        self.assertEqual(len(matches), 1)

    def test_non_latin_titles_remain_distinct(self):
        self.assertNotEqual(
            matcher.normalize_title("北京乐队"), matcher.normalize_title("上海乐队")
        )
        self.assertEqual(matcher.normalize_text("Artist w/ Guest"), "artist with guest")

    def test_output_cannot_overwrite_source_inputs(self):
        config = matcher.MatchingConfig(stubhub_csv="input.csv", output_csv="input.csv")
        with self.assertRaises(ValueError):
            matcher.run(config)

    def test_write_matches_csv_does_not_create_review_file(self) -> None:
        matches = matcher.find_high_confidence_matches([stubhub_row()], [ticketmaster_row()])

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "matches.csv"
            matcher.write_matches_csv(matches, output_path)

            with output_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))

            self.assertEqual(rows[0]["stubhub_eventId"], "sh-1")
            self.assertFalse((Path(tmpdir) / "intersection_review.csv").exists())


if __name__ == "__main__":
    unittest.main()
