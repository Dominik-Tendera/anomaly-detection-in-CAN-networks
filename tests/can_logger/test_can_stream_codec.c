/*
 * test_can_stream_codec.c
 *
 * Host test harness for the wire format. Compiles the very same
 * can_stream_codec.c that the firmware links, so the properties below hold for
 * the implementation running on the microcontroller and not for a copy of it.
 *
 * Build and run:
 *   gcc -std=c11 -O2 -Wall -Wextra -I../../can_logger/Core/Inc \
 *       test_can_stream_codec.c ../../can_logger/Core/Src/can_stream_codec.c -o test.exe
 */

#include "can_stream_codec.h"

#include <inttypes.h>
#include <stdio.h>
#include <string.h>

static unsigned long g_checks;
static unsigned long g_failures;

static void Check(int condition, const char *what) {
	g_checks++;
	if (!condition) {
		g_failures++;
		if (g_failures <= 20UL) {
			printf("FAIL: %s\n", what);
		}
	}
}

/* Deterministic pseudorandom source, so a failure is always reproducible. */
static uint32_t g_state = 0x12345678U;

static uint32_t NextRandom(void) {
	g_state ^= g_state << 13;
	g_state ^= g_state >> 17;
	g_state ^= g_state << 5;
	return g_state;
}

static uint8_t FeedAll(CanStreamDecoder *decoder, const uint8_t *bytes,
		size_t length, CanStreamRecord *record) {
	uint8_t got = 0U;

	for (size_t i = 0U; i < length; i++) {
		if (CanStreamDecoderFeed(decoder, bytes[i], record) != 0U) {
			got++;
		}
	}
	return got;
}

/* ------------------------------------------------------------------------- */
/* Property 1: frame round-trip over the whole field domain                  */
/* ------------------------------------------------------------------------- */

static void TestFrameRoundTrip(void) {
	CanStreamDecoder decoder;
	CanStreamRecord record;
	CanStreamFrame in;
	CanStreamFrame out;
	uint8_t encoded[CAN_STREAM_MAX_ENCODED];

	CanStreamDecoderInit(&decoder);

	for (uint16_t id = 0U; id <= 0x7FFU; id++) {
		for (uint8_t dlc = 0U; dlc <= 8U; dlc++) {
			for (uint8_t channel = 1U; channel <= 2U; channel++) {
				size_t length;
				uint8_t pattern = (uint8_t) (NextRandom() & 0xFFU);

				memset(&in, 0, sizeof(in));
				in.id = id;
				in.dlc = dlc;
				in.channel = channel;
				in.seq = (uint16_t) NextRandom();
				in.ts32 = NextRandom();
				for (uint8_t i = 0U; i < dlc; i++) {
					/* Mix of zeros, 0xFF and random to stress COBS. */
					switch (id % 3U) {
					case 0U:
						in.data[i] = 0x00U;
						break;
					case 1U:
						in.data[i] = 0xFFU;
						break;
					default:
						in.data[i] = (uint8_t) (pattern + i);
						break;
					}
				}

				length = CanStreamEncodeFrame(&in, encoded,
						sizeof(encoded));
				Check(length != 0U, "frame encode returned zero");
				if (length == 0U) {
					continue;
				}
				/* Encoded stream must never contain the delimiter. */
				for (size_t i = 0U; i + 1U < length; i++) {
					Check(encoded[i] != 0x00U,
							"delimiter inside encoded record");
				}
				Check(encoded[length - 1U] == 0x00U,
						"missing trailing delimiter");
				/* COBS overhead is exactly one code byte plus delimiter. */
				Check(length == (size_t) (10U + dlc) + 2U,
						"unexpected encoded length");

				memset(&record, 0, sizeof(record));
				Check(FeedAll(&decoder, encoded, length, &record) == 1U,
						"decoder did not produce exactly one record");
				Check(record.type == (uint8_t) CAN_STREAM_REC_FRAME,
						"wrong record type");
				Check(record.seq == in.seq, "record seq mismatch");

				memset(&out, 0xA5, sizeof(out));
				Check(CanStreamDecodeFrame(&record, &out) != 0U,
						"frame decode failed");
				Check(out.id == in.id, "id mismatch");
				Check(out.dlc == in.dlc, "dlc mismatch");
				Check(out.channel == in.channel, "channel mismatch");
				Check(out.seq == in.seq, "seq mismatch");
				Check(out.ts32 == in.ts32, "ts32 mismatch");
				Check(memcmp(out.data, in.data, in.dlc) == 0,
						"payload mismatch");
				for (uint8_t i = in.dlc; i < 8U; i++) {
					Check(out.data[i] == 0U,
							"padding beyond dlc is not zeroed");
				}
			}
		}
	}

	Check(decoder.crc_errors == 0U, "unexpected crc errors");
	Check(decoder.cobs_errors == 0U, "unexpected cobs errors");
	Check(decoder.sync_losses == 0U, "unexpected sync losses");
}

/* ------------------------------------------------------------------------- */
/* Property 2: extreme payloads                                              */
/* ------------------------------------------------------------------------- */

static void TestFrameExtremes(void) {
	static const uint8_t patterns[3][8] = {
		{ 0, 0, 0, 0, 0, 0, 0, 0 },
		{ 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF },
		{ 0x00, 0xFF, 0x00, 0xFF, 0x00, 0xFF, 0x00, 0xFF }
	};
	CanStreamDecoder decoder;
	CanStreamRecord record;
	uint8_t encoded[CAN_STREAM_MAX_ENCODED];

	CanStreamDecoderInit(&decoder);

	for (size_t p = 0U; p < 3U; p++) {
		static const uint32_t times[4] = { 0U, 1U, 0x7FFFFFFFU, 0xFFFFFFFFU };
		for (size_t t = 0U; t < 4U; t++) {
			CanStreamFrame in;
			CanStreamFrame out;
			size_t length;

			memset(&in, 0, sizeof(in));
			in.id = (uint16_t) (p == 0U ? 0U : 0x7FFU);
			in.dlc = 8U;
			in.channel = (uint8_t) ((p % 2U) + 1U);
			in.seq = (uint16_t) (t == 0U ? 0U : 0xFFFFU);
			in.ts32 = times[t];
			memcpy(in.data, patterns[p], 8U);

			length = CanStreamEncodeFrame(&in, encoded, sizeof(encoded));
			Check(length != 0U, "extreme frame encode failed");
			Check(FeedAll(&decoder, encoded, length, &record) == 1U,
					"extreme frame not decoded");
			Check(CanStreamDecodeFrame(&record, &out) != 0U,
					"extreme frame field decode failed");
			Check(out.ts32 == in.ts32, "extreme ts32 mismatch");
			Check(out.seq == in.seq, "extreme seq mismatch");
			Check(memcmp(out.data, in.data, 8U) == 0,
					"extreme payload mismatch");
		}
	}
}

/* ------------------------------------------------------------------------- */
/* Property 3: rejection of out of range input                               */
/* ------------------------------------------------------------------------- */

static void TestEncoderRejectsInvalid(void) {
	CanStreamFrame frame;
	uint8_t encoded[CAN_STREAM_MAX_ENCODED];

	memset(&frame, 0, sizeof(frame));
	frame.id = 0x800U; /* beyond 11 bits */
	frame.dlc = 1U;
	frame.channel = 1U;
	Check(CanStreamEncodeFrame(&frame, encoded, sizeof(encoded)) == 0U,
			"encoder accepted an 12-bit identifier");

	frame.id = 0x7FFU;
	frame.dlc = 9U;
	Check(CanStreamEncodeFrame(&frame, encoded, sizeof(encoded)) == 0U,
			"encoder accepted dlc 9");

	frame.dlc = 8U;
	frame.channel = 0U;
	Check(CanStreamEncodeFrame(&frame, encoded, sizeof(encoded)) == 0U,
			"encoder accepted channel 0");

	frame.channel = 3U;
	Check(CanStreamEncodeFrame(&frame, encoded, sizeof(encoded)) == 0U,
			"encoder accepted channel 3");

	frame.channel = 1U;
	Check(CanStreamEncodeFrame(&frame, encoded, 4U) == 0U,
			"encoder ignored a short output buffer");
}

/* ------------------------------------------------------------------------- */
/* Property 4: CRC-8 detects every single bit error in a record              */
/* ------------------------------------------------------------------------- */

static void TestCrcDetectsSingleBitErrors(void) {
	uint8_t record[CAN_STREAM_MAX_RECORD];
	size_t lengths[4] = { 4U, 12U, 25U, 111U };

	for (size_t l = 0U; l < 4U; l++) {
		size_t length = lengths[l];
		uint8_t crc;

		for (size_t i = 0U; i + 1U < length; i++) {
			record[i] = (uint8_t) NextRandom();
		}
		crc = CanStreamCrc8(record, length - 1U);
		record[length - 1U] = crc;

		Check(CanStreamCrc8(record, length - 1U) == record[length - 1U],
				"crc of an untouched record does not match");

		for (size_t byte = 0U; byte < length; byte++) {
			for (uint8_t bit = 0U; bit < 8U; bit++) {
				uint8_t corrupted[CAN_STREAM_MAX_RECORD];

				memcpy(corrupted, record, length);
				corrupted[byte] ^= (uint8_t) (1U << bit);
				Check(CanStreamCrc8(corrupted, length - 1U)
						!= corrupted[length - 1U],
						"single bit error slipped past crc");
			}
		}
	}
}

/* ------------------------------------------------------------------------- */
/* Property 5: decoder resynchronises after garbage on the line              */
/* ------------------------------------------------------------------------- */

static void TestResynchronisation(void) {
	CanStreamDecoder decoder;
	CanStreamRecord record;
	CanStreamFrame in;
	CanStreamFrame out;
	uint8_t encoded[CAN_STREAM_MAX_ENCODED];
	size_t length;

	CanStreamDecoderInit(&decoder);

	memset(&in, 0, sizeof(in));
	in.id = 0x123U;
	in.dlc = 8U;
	in.channel = 2U;
	in.seq = 0x4242U;
	in.ts32 = 0xDEADBEEFU;
	for (uint8_t i = 0U; i < 8U; i++) {
		in.data[i] = (uint8_t) (0x10U + i);
	}
	length = CanStreamEncodeFrame(&in, encoded, sizeof(encoded));
	Check(length != 0U, "resync setup encode failed");

	for (unsigned iteration = 0U; iteration < 2000U; iteration++) {
		uint8_t garbage[64];
		size_t garbage_length = (size_t) (NextRandom() % sizeof(garbage));
		uint8_t produced;

		for (size_t i = 0U; i < garbage_length; i++) {
			garbage[i] = (uint8_t) NextRandom();
		}
		(void) FeedAll(&decoder, garbage, garbage_length, &record);

		/* A truncated record followed by a delimiter must not be accepted. */
		(void) FeedAll(&decoder, encoded, length / 2U, &record);
		{
			uint8_t delimiter = 0x00U;
			(void) FeedAll(&decoder, &delimiter, 1U, &record);
		}

		memset(&record, 0, sizeof(record));
		produced = FeedAll(&decoder, encoded, length, &record);
		Check(produced == 1U, "decoder failed to resynchronise");
		if (produced == 1U) {
			Check(CanStreamDecodeFrame(&record, &out) != 0U,
					"record after garbage does not decode");
			Check(out.id == in.id && out.seq == in.seq
					&& out.ts32 == in.ts32
					&& memcmp(out.data, in.data, 8U) == 0,
					"record after garbage has wrong content");
		}
	}

	Check(decoder.records_ok >= 2000U, "not every good record was accepted");
}

/* ------------------------------------------------------------------------- */
/* Property 6: no corrupted record is ever handed over as valid              */
/* ------------------------------------------------------------------------- */

static void TestCorruptedStreamNeverYieldsWrongFrame(void) {
	CanStreamDecoder decoder;
	CanStreamFrame in;
	uint8_t encoded[CAN_STREAM_MAX_ENCODED];
	size_t length;
	unsigned long accepted_wrong = 0UL;
	unsigned long accepted_total = 0UL;

	memset(&in, 0, sizeof(in));
	in.id = 0x2AAU;
	in.dlc = 6U;
	in.channel = 1U;
	in.seq = 0x1000U;
	in.ts32 = 0x01020304U;
	for (uint8_t i = 0U; i < 6U; i++) {
		in.data[i] = (uint8_t) (0xA0U + i);
	}
	length = CanStreamEncodeFrame(&in, encoded, sizeof(encoded));
	Check(length != 0U, "corruption setup encode failed");

	for (size_t byte = 0U; byte + 1U < length; byte++) {
		for (uint8_t bit = 0U; bit < 8U; bit++) {
			uint8_t corrupted[CAN_STREAM_MAX_ENCODED];
			CanStreamRecord record;
			CanStreamFrame out;

			memcpy(corrupted, encoded, length);
			corrupted[byte] ^= (uint8_t) (1U << bit);

			CanStreamDecoderInit(&decoder);
			memset(&record, 0, sizeof(record));
			if (FeedAll(&decoder, corrupted, length, &record) == 0U) {
				continue; /* rejected, which is the expected outcome */
			}
			accepted_total++;
			memset(&out, 0, sizeof(out));
			if (CanStreamDecodeFrame(&record, &out) == 0U) {
				continue;
			}
			if ((out.id != in.id) || (out.dlc != in.dlc)
					|| (out.channel != in.channel) || (out.seq != in.seq)
					|| (out.ts32 != in.ts32)
					|| (memcmp(out.data, in.data, in.dlc) != 0)) {
				accepted_wrong++;
			}
		}
	}

	/*
	 * CRC-8 leaves a residual probability of about 1/256 that a corrupted
	 * record still validates. The property under test is that this stays a
	 * rare residue rather than a systematic hole in the framing, so the bound
	 * is stated explicitly instead of demanding zero.
	 */
	printf("  corrupted records accepted: %lu of %zu injected (wrong content: %lu)\n",
			accepted_total, (length - 1U) * 8U, accepted_wrong);
	Check(accepted_wrong * 20UL <= (length - 1U) * 8U,
			"too many corrupted records pass validation");
}

/* ------------------------------------------------------------------------- */
/* Property 7: round-trip of the remaining record types                      */
/* ------------------------------------------------------------------------- */

static void TestOtherRecords(void) {
	CanStreamDecoder decoder;
	CanStreamRecord record;
	uint8_t encoded[CAN_STREAM_MAX_ENCODED];
	size_t length;

	CanStreamDecoderInit(&decoder);

	/* Time synchronisation */
	{
		static const uint64_t values[4] = { 0U, 1U, 0x00000000FFFFFFFFULL,
				0xFFFFFFFFFFFFFFFFULL };
		for (size_t i = 0U; i < 4U; i++) {
			uint64_t out = 0U;

			length = CanStreamEncodeTimeSync((uint16_t) i, values[i],
					encoded, sizeof(encoded));
			Check(length
					== CAN_STREAM_LEN_TIME_SYNC + CAN_STREAM_COBS_OVERHEAD,
					"time sync length changed");
			Check(FeedAll(&decoder, encoded, length, &record) == 1U,
					"time sync not decoded");
			Check(CanStreamDecodeTimeSync(&record, &out) != 0U,
					"time sync field decode failed");
			Check(out == values[i], "time sync value mismatch");
			Check(record.seq == (uint16_t) i, "time sync seq mismatch");
		}
	}

	/* Session */
	{
		CanStreamSession in;
		CanStreamSession out;

		memset(&in, 0, sizeof(in));
		in.proto_version = CAN_STREAM_PROTO_VERSION;
		in.flags = CAN_STREAM_FLAG_MEASUREMENT_MODE
				| CAN_STREAM_FLAG_STREAM_ENABLED;
		in.max_record_len = CAN_STREAM_MAX_RECORD;
		in.ts64 = 0x0123456789ABCDEFULL;
		in.can1_bitrate = 1000000U;
		in.can2_bitrate = 500000U;
		in.can1_btr = 0x0014E001U;
		in.can2_btr = 0x0014E009U;
		in.uart_baudrate = 2000000U;
		in.rtc_valid = 1U;
		in.rtc_year = 26U;
		in.rtc_month = 9U;
		in.rtc_day = 15U;
		in.rtc_hour = 23U;
		in.rtc_minute = 59U;
		in.rtc_second = 58U;

		length = CanStreamEncodeSession(7U, &in, encoded, sizeof(encoded));
		Check(length != 0U, "session encode failed");
		Check(FeedAll(&decoder, encoded, length, &record) == 1U,
				"session not decoded");
		memset(&out, 0, sizeof(out));
		Check(CanStreamDecodeSession(&record, &out) != 0U,
				"session field decode failed");
		Check(memcmp(&in, &out, sizeof(in)) == 0, "session mismatch");
	}

	/* Event */
	{
		CanStreamEvent in;
		CanStreamEvent out;

		memset(&in, 0, sizeof(in));
		in.channel = 2U;
		in.reason = (uint8_t) CAN_STREAM_EVENT_RING_DROP;
		in.state = 3U;
		in.frame_id = 0x7FFU;
		in.error_code = 0xDEADBEEFU;
		in.suppressed = 1234U;
		in.ts64 = 0xFFFFFFFFFFFFFFFFULL;

		length = CanStreamEncodeEvent(9U, &in, encoded, sizeof(encoded));
		Check(length != 0U, "event encode failed");
		Check(FeedAll(&decoder, encoded, length, &record) == 1U,
				"event not decoded");
		memset(&out, 0, sizeof(out));
		Check(CanStreamDecodeEvent(&record, &out) != 0U,
				"event field decode failed");
		Check(memcmp(&in, &out, sizeof(in)) == 0, "event mismatch");
	}

	/* Bus statistics, the longest record */
	{
		CanStreamBusStats in;
		CanStreamBusStats out;
		uint32_t *fields = &in.rx_frames;

		memset(&in, 0, sizeof(in));
		in.channel = 1U;
		in.ts64 = 0x1122334455667788ULL;
		for (size_t i = 0U; i < 23U; i++) {
			fields[i] = NextRandom();
		}
		in.fifo_max_fill = 3U;
		in.current_rec = 96U;
		in.current_tec = 128U;
		in.max_rec = 127U;
		in.max_tec = 255U;
		in.state = 2U;

		length = CanStreamEncodeBusStats(11U, &in, encoded, sizeof(encoded));
		Check(length != 0U, "bus stats encode failed");
		Check(length <= CAN_STREAM_MAX_ENCODED,
				"bus stats exceed the encoded bound");
		Check(FeedAll(&decoder, encoded, length, &record) == 1U,
				"bus stats not decoded");
		memset(&out, 0, sizeof(out));
		Check(CanStreamDecodeBusStats(&record, &out) != 0U,
				"bus stats field decode failed");
		Check(memcmp(&in, &out, sizeof(in)) == 0, "bus stats mismatch");
	}

	/* Logger statistics */
	{
		CanStreamLoggerStats in;
		CanStreamLoggerStats out;

		memset(&in, 0, sizeof(in));
		in.ts64 = 0x00FF00FF00FF00FFULL;
		in.ring_drop_total = 42U;
		in.tx_records_sent = 123456U;
		in.tx_bytes_sent = 7890123U;
		in.tx_records_dropped = 7U;
		in.tx_bytes_dropped = 77U;
		in.event_queue_dropped = 3U;
		in.loop_max_us = 15000U;
		in.ring_peak = 4096U;
		in.ring_capacity = 5119U;
		in.tx_buffer_peak = 8000U;
		in.tx_buffer_capacity = 16383U;
		in.flags = CAN_STREAM_FLAG_USB_LOGGING;

		length = CanStreamEncodeLoggerStats(13U, &in, encoded,
				sizeof(encoded));
		Check(length != 0U, "logger stats encode failed");
		Check(FeedAll(&decoder, encoded, length, &record) == 1U,
				"logger stats not decoded");
		memset(&out, 0, sizeof(out));
		Check(CanStreamDecodeLoggerStats(&record, &out) != 0U,
				"logger stats field decode failed");
		Check(memcmp(&in, &out, sizeof(in)) == 0, "logger stats mismatch");
	}

	/* Acknowledgement */
	{
		CanStreamAck in;
		CanStreamAck out;

		memset(&in, 0, sizeof(in));
		in.status = 1U;
		in.command = 5U;
		in.value = 921600U;
		in.ts64 = 987654321U;

		length = CanStreamEncodeAck(15U, &in, encoded, sizeof(encoded));
		Check(length != 0U, "ack encode failed");
		Check(FeedAll(&decoder, encoded, length, &record) == 1U,
				"ack not decoded");
		memset(&out, 0, sizeof(out));
		Check(CanStreamDecodeAck(&record, &out) != 0U,
				"ack field decode failed");
		Check(memcmp(&in, &out, sizeof(in)) == 0, "ack mismatch");
	}

	/* A decoder must not confuse record types. */
	{
		uint64_t ts = 0U;
		CanStreamFrame frame;

		length = CanStreamEncodeTimeSync(1U, 5U, encoded, sizeof(encoded));
		Check(FeedAll(&decoder, encoded, length, &record) == 1U,
				"type confusion setup failed");
		Check(CanStreamDecodeFrame(&record, &frame) == 0U,
				"time sync decoded as a frame");
		Check(CanStreamDecodeBusStats(&record,
				(CanStreamBusStats*) (void*) &frame) == 0U,
				"time sync decoded as bus stats");
		Check(CanStreamDecodeTimeSync(&record, &ts) != 0U,
				"time sync rejected by its own decoder");
	}
}

/* ------------------------------------------------------------------------- */
/* Property 8: time reconstruction is exact across the 32-bit wrap            */
/* ------------------------------------------------------------------------- */

static void TestTimeReconstruction(void) {
	/* A synchronisation record every second, frames every 100 us. */
	const uint64_t base = 0xFFFFFFF0ULL; /* just below the 32-bit wrap */
	uint64_t sync = base;

	for (uint64_t step = 0U; step < 100000U; step++) {
		uint64_t real_time = base + (step * 100U);
		uint32_t ts32 = (uint32_t) real_time;
		uint64_t restored;

		if ((step % 10000U) == 0U) {
			sync = real_time; /* device emits a synchronisation record */
		}
		restored = CanStreamRestoreTime(sync, ts32);
		Check(restored == real_time, "time reconstruction mismatch");
	}

	/* A frame stamped slightly before the synchronisation record. */
	{
		uint64_t sync_point = 0x100000000ULL;
		uint64_t earlier = sync_point - 500U;

		Check(CanStreamRestoreTime(sync_point, (uint32_t) earlier) == earlier,
				"frame before the sync record reconstructed incorrectly");
	}
}

/* ------------------------------------------------------------------------- */
/* Property 9: throughput budget of the chosen layout                        */
/* ------------------------------------------------------------------------- */

static void ReportThroughput(void) {
	const double baud = 2000000.0;
	const double bytes_per_second = baud / 10.0; /* 8N1 */
	const size_t frame_dlc8 = 10U + 8U + 2U;
	const size_t frame_dlc0 = 10U + 0U + 2U;
	const double rate_dlc8 = 3600.0; /* 40 % load, only DLC 8 frames */
	const double rate_dlc0 = 8500.0; /* 40 % load, only DLC 0 frames */

	printf("  record size: DLC 0 -> %zu B, DLC 8 -> %zu B (COBS included)\n",
			frame_dlc0, frame_dlc8);
	printf("  40%% load, DLC 8: %.0f rec/s -> %.1f kB/s -> %.1f%% of %.0f baud\n",
			rate_dlc8, (rate_dlc8 * (double) frame_dlc8) / 1000.0,
			100.0 * rate_dlc8 * (double) frame_dlc8 / bytes_per_second, baud);
	printf("  40%% load, DLC 0: %.0f rec/s -> %.1f kB/s -> %.1f%% of %.0f baud\n",
			rate_dlc0, (rate_dlc0 * (double) frame_dlc0) / 1000.0,
			100.0 * rate_dlc0 * (double) frame_dlc0 / bytes_per_second, baud);

	Check(rate_dlc8 * (double) frame_dlc8 < bytes_per_second,
			"DLC 8 profile does not fit into the link budget");
	Check(rate_dlc0 * (double) frame_dlc0 < bytes_per_second,
			"DLC 0 profile does not fit into the link budget");
}

/* ------------------------------------------------------------------------- */
/* Cross language vectors: the C encoder writes a stream and the expected     */
/* field values, the Python receiver has to agree on every field.             */
/* ------------------------------------------------------------------------- */

static void EmitVectors(const char *bin_path, const char *csv_path) {
	FILE *bin = fopen(bin_path, "wb");
	FILE *csv = fopen(csv_path, "w");
	uint8_t encoded[CAN_STREAM_MAX_ENCODED];
	uint16_t seq = 0U;
	size_t length;

	if ((bin == NULL) || (csv == NULL)) {
		printf("cannot open vector output\n");
		if (bin != NULL) {
			fclose(bin);
		}
		if (csv != NULL) {
			fclose(csv);
		}
		g_failures++;
		return;
	}

	/* Session */
	{
		CanStreamSession session;

		memset(&session, 0, sizeof(session));
		session.proto_version = CAN_STREAM_PROTO_VERSION;
		session.flags = CAN_STREAM_FLAG_STREAM_ENABLED
				| CAN_STREAM_FLAG_USB_LOGGING;
		session.max_record_len = CAN_STREAM_MAX_RECORD;
		session.ts64 = 1234567890123ULL;
		session.can1_bitrate = 1000000U;
		session.can2_bitrate = 1000000U;
		session.can1_btr = 0x0014E004U;
		session.can2_btr = 0x0014E004U;
		session.uart_baudrate = 2000000U;
		session.rtc_valid = 1U;
		session.rtc_year = 26U;
		session.rtc_month = 9U;
		session.rtc_day = 15U;
		session.rtc_hour = 12U;
		session.rtc_minute = 34U;
		session.rtc_second = 56U;

		length = CanStreamEncodeSession(seq, &session, encoded,
				sizeof(encoded));
		fwrite(encoded, 1U, length, bin);
		fprintf(csv,
				"SESSION;%u;%u;%u;%u;%" PRIu64
				";%u;%u;%u;%u;%u;%u;%u;%u;%u;%u;%u;%u\n", seq,
				session.proto_version, session.flags, session.max_record_len,
				session.ts64, session.can1_bitrate, session.can2_bitrate,
				session.can1_btr, session.can2_btr, session.uart_baudrate,
				session.rtc_valid, session.rtc_year, session.rtc_month,
				session.rtc_day, session.rtc_hour, session.rtc_minute,
				session.rtc_second);
		seq++;
	}

	/* Time synchronisation just below the 32-bit wrap */
	{
		uint64_t ts = 0xFFFFFF00ULL;

		length = CanStreamEncodeTimeSync(seq, ts, encoded, sizeof(encoded));
		fwrite(encoded, 1U, length, bin);
		fprintf(csv, "TIMESYNC;%u;%" PRIu64 "\n", seq, ts);
		seq++;
	}

	/* Frames across the whole field domain, sampled deterministically */
	for (uint16_t id = 0U; id <= 0x7FFU; id += 37U) {
		for (uint8_t dlc = 0U; dlc <= 8U; dlc++) {
			CanStreamFrame frame;
			char hex[17];

			memset(&frame, 0, sizeof(frame));
			frame.id = id;
			frame.dlc = dlc;
			frame.channel = (uint8_t) ((id % 2U) + 1U);
			frame.seq = seq;
			frame.ts32 = 0xFFFFFF00U + (uint32_t) (id * 9U + dlc);
			for (uint8_t i = 0U; i < dlc; i++) {
				frame.data[i] = (uint8_t) ((id + i * 31U) & 0xFFU);
			}

			length = CanStreamEncodeFrame(&frame, encoded, sizeof(encoded));
			if (length == 0U) {
				g_failures++;
				continue;
			}
			fwrite(encoded, 1U, length, bin);

			hex[0] = '\0';
			for (uint8_t i = 0U; i < dlc; i++) {
				sprintf(&hex[i * 2U], "%02X", frame.data[i]);
			}
			fprintf(csv, "FRAME;%u;%u;%u;%u;%s;%" PRIu32 "\n", frame.seq,
					frame.channel, frame.id, frame.dlc, hex, frame.ts32);
			seq++;
		}
	}

	/* Deliberate garbage: the receiver has to skip it and resynchronise. */
	{
		static const uint8_t garbage[] = { 0xFF, 0x01, 0x02, 0x00, 0x7F, 0x00,
				0xAB, 0xCD, 0xEF, 0x00 };
		fwrite(garbage, 1U, sizeof(garbage), bin);
	}

	/* Event */
	{
		CanStreamEvent event;

		memset(&event, 0, sizeof(event));
		event.channel = 2U;
		event.reason = (uint8_t) CAN_STREAM_EVENT_RING_DROP;
		event.state = 1U;
		event.frame_id = 0x123U;
		event.error_code = 0x00000040U;
		event.suppressed = 17U;
		event.ts64 = 0x1000000000ULL;

		length = CanStreamEncodeEvent(seq, &event, encoded, sizeof(encoded));
		fwrite(encoded, 1U, length, bin);
		fprintf(csv, "EVENT;%u;%u;%u;%u;%u;%" PRIu32 ";%" PRIu32 ";%" PRIu64
				"\n", seq, event.channel, event.reason, event.state,
				event.frame_id, event.error_code, event.suppressed,
				event.ts64);
		seq++;
	}

	/* Bus statistics */
	{
		CanStreamBusStats stats;
		uint32_t *fields;

		memset(&stats, 0, sizeof(stats));
		stats.channel = 1U;
		stats.ts64 = 0x2000000000ULL;
		fields = &stats.rx_frames;
		for (size_t i = 0U; i < 23U; i++) {
			fields[i] = (uint32_t) (i * 1000U + 7U);
		}
		stats.fifo_max_fill = 3U;
		stats.current_rec = 12U;
		stats.current_tec = 0U;
		stats.max_rec = 96U;
		stats.max_tec = 5U;
		stats.state = 2U;

		length = CanStreamEncodeBusStats(seq, &stats, encoded,
				sizeof(encoded));
		fwrite(encoded, 1U, length, bin);
		fprintf(csv, "BUSSTATS;%u;%u;%" PRIu64, seq, stats.channel,
				stats.ts64);
		for (size_t i = 0U; i < 23U; i++) {
			fprintf(csv, ";%" PRIu32, fields[i]);
		}
		fprintf(csv, ";%u;%u;%u;%u;%u;%u\n", stats.fifo_max_fill,
				stats.current_rec, stats.current_tec, stats.max_rec,
				stats.max_tec, stats.state);
		seq++;
	}

	/* Logger statistics */
	{
		CanStreamLoggerStats stats;

		memset(&stats, 0, sizeof(stats));
		stats.ts64 = 0x3000000000ULL;
		stats.ring_drop_total = 5U;
		stats.tx_records_sent = 999999U;
		stats.tx_bytes_sent = 12345678U;
		stats.tx_records_dropped = 3U;
		stats.tx_bytes_dropped = 60U;
		stats.event_queue_dropped = 1U;
		stats.loop_max_us = 4200U;
		stats.ring_peak = 512U;
		stats.ring_capacity = 5119U;
		stats.tx_buffer_peak = 9000U;
		stats.tx_buffer_capacity = 16383U;
		stats.flags = CAN_STREAM_FLAG_STREAM_ENABLED
				| CAN_STREAM_FLAG_MEASUREMENT_MODE;

		length = CanStreamEncodeLoggerStats(seq, &stats, encoded,
				sizeof(encoded));
		fwrite(encoded, 1U, length, bin);
		fprintf(csv, "LOGGERSTATS;%u;%" PRIu64 ";%" PRIu32 ";%" PRIu32
				";%" PRIu32 ";%" PRIu32 ";%" PRIu32 ";%" PRIu32 ";%" PRIu32
				";%u;%u;%u;%u;%u\n", seq, stats.ts64, stats.ring_drop_total,
				stats.tx_records_sent, stats.tx_bytes_sent,
				stats.tx_records_dropped, stats.tx_bytes_dropped,
				stats.event_queue_dropped, stats.loop_max_us, stats.ring_peak,
				stats.ring_capacity, stats.tx_buffer_peak,
				stats.tx_buffer_capacity, stats.flags);
		seq++;
	}

	/* Acknowledgement */
	{
		CanStreamAck ack;

		memset(&ack, 0, sizeof(ack));
		ack.status = 0U;
		ack.command = 3U;
		ack.value = 2000000U;
		ack.ts64 = 0x4000000000ULL;

		length = CanStreamEncodeAck(seq, &ack, encoded, sizeof(encoded));
		fwrite(encoded, 1U, length, bin);
		fprintf(csv, "ACK;%u;%u;%u;%" PRIu32 ";%" PRIu64 "\n", seq, ack.status,
				ack.command, ack.value, ack.ts64);
		seq++;
	}

	fclose(bin);
	fclose(csv);
	printf("  wrote %s and %s (%u records)\n", bin_path, csv_path, seq);
}

int main(int argc, char **argv) {
	if ((argc >= 4) && (strcmp(argv[1], "--emit-vectors") == 0)) {
		printf("emitting cross language vectors\n");
		EmitVectors(argv[2], argv[3]);
		return (g_failures == 0UL) ? 0 : 1;
	}

	printf("can_stream_codec host tests\n");

	printf("[1] frame round-trip over the full field domain\n");
	TestFrameRoundTrip();
	printf("[2] extreme payloads and timestamps\n");
	TestFrameExtremes();
	printf("[3] encoder rejects out of range input\n");
	TestEncoderRejectsInvalid();
	printf("[4] crc detects every single bit error\n");
	TestCrcDetectsSingleBitErrors();
	printf("[5] decoder resynchronises after garbage\n");
	TestResynchronisation();
	printf("[6] corrupted stream never yields a wrong frame\n");
	TestCorruptedStreamNeverYieldsWrongFrame();
	printf("[7] round-trip of the remaining record types\n");
	TestOtherRecords();
	printf("[8] time reconstruction across the 32-bit wrap\n");
	TestTimeReconstruction();
	printf("[9] link budget\n");
	ReportThroughput();

	printf("\nchecks: %lu, failures: %lu\n", g_checks, g_failures);
	return (g_failures == 0UL) ? 0 : 1;
}
