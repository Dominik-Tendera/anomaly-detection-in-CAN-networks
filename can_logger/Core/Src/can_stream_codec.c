/*
 * can_stream_codec.c
 *
 * See can_stream_codec.h. No HAL dependency on purpose: this file is compiled
 * both into the firmware and into the host test harness, so the tests exercise
 * the implementation that actually runs on the microcontroller.
 */

#include "can_stream_codec.h"

#include <string.h>

#define CAN_STREAM_CRC8_POLY 0x07U

/* ------------------------------------------------------------------------- */
/* Little endian helpers                                                     */
/* ------------------------------------------------------------------------- */

static void PutU8(uint8_t *buffer, size_t *offset, uint8_t value) {
	buffer[*offset] = value;
	*offset += 1U;
}

static void PutU16(uint8_t *buffer, size_t *offset, uint16_t value) {
	buffer[*offset] = (uint8_t) (value & 0xFFU);
	buffer[*offset + 1U] = (uint8_t) ((value >> 8) & 0xFFU);
	*offset += 2U;
}

static void PutU32(uint8_t *buffer, size_t *offset, uint32_t value) {
	buffer[*offset] = (uint8_t) (value & 0xFFU);
	buffer[*offset + 1U] = (uint8_t) ((value >> 8) & 0xFFU);
	buffer[*offset + 2U] = (uint8_t) ((value >> 16) & 0xFFU);
	buffer[*offset + 3U] = (uint8_t) ((value >> 24) & 0xFFU);
	*offset += 4U;
}

static void PutU64(uint8_t *buffer, size_t *offset, uint64_t value) {
	for (size_t i = 0U; i < 8U; i++) {
		buffer[*offset + i] = (uint8_t) ((value >> (8U * i)) & 0xFFU);
	}
	*offset += 8U;
}

static uint8_t GetU8(const uint8_t *buffer, size_t *offset) {
	uint8_t value = buffer[*offset];
	*offset += 1U;
	return value;
}

static uint16_t GetU16(const uint8_t *buffer, size_t *offset) {
	uint16_t value = (uint16_t) buffer[*offset]
			| (uint16_t) ((uint16_t) buffer[*offset + 1U] << 8);
	*offset += 2U;
	return value;
}

static uint32_t GetU32(const uint8_t *buffer, size_t *offset) {
	uint32_t value = (uint32_t) buffer[*offset]
			| ((uint32_t) buffer[*offset + 1U] << 8)
			| ((uint32_t) buffer[*offset + 2U] << 16)
			| ((uint32_t) buffer[*offset + 3U] << 24);
	*offset += 4U;
	return value;
}

static uint64_t GetU64(const uint8_t *buffer, size_t *offset) {
	uint64_t value = 0U;
	for (size_t i = 0U; i < 8U; i++) {
		value |= (uint64_t) buffer[*offset + i] << (8U * i);
	}
	*offset += 8U;
	return value;
}

/* ------------------------------------------------------------------------- */
/* CRC-8, polynomial 0x07, initial value 0x00, no reflection                 */
/* ------------------------------------------------------------------------- */

uint8_t CanStreamCrc8(const uint8_t *data, size_t length) {
	uint8_t crc = 0U;

	if (data == NULL) {
		return 0U;
	}
	for (size_t i = 0U; i < length; i++) {
		crc ^= data[i];
		for (uint8_t bit = 0U; bit < 8U; bit++) {
			if ((crc & 0x80U) != 0U) {
				crc = (uint8_t) ((uint8_t) (crc << 1)
						^ CAN_STREAM_CRC8_POLY);
			} else {
				crc = (uint8_t) (crc << 1);
			}
		}
	}
	return crc;
}

/* ------------------------------------------------------------------------- */
/* COBS                                                                      */
/* ------------------------------------------------------------------------- */

size_t CanStreamCobsEncode(const uint8_t *input, size_t length,
		uint8_t *output, size_t output_size) {
	size_t code_index = 0U;
	size_t write = 1U;
	uint8_t code = 1U;

	if ((input == NULL) || (output == NULL) || (length == 0U)) {
		return 0U;
	}
	if (output_size < (length + 3U)) {
		return 0U;
	}

	for (size_t i = 0U; i < length; i++) {
		if (input[i] != 0U) {
			output[write] = input[i];
			write++;
			code++;
		}
		if ((input[i] == 0U) || (code == 0xFFU)) {
			output[code_index] = code;
			code_index = write;
			write++;
			code = 1U;
		}
	}
	output[code_index] = code;
	output[write] = 0x00U;
	write++;

	return write;
}

size_t CanStreamCobsDecode(const uint8_t *input, size_t length,
		uint8_t *output, size_t output_size) {
	size_t read = 0U;
	size_t write = 0U;

	if ((input == NULL) || (output == NULL) || (length == 0U)) {
		return 0U;
	}

	while (read < length) {
		uint8_t code = input[read];
		read++;

		if (code == 0U) {
			return 0U; /* delimiter must never appear inside a record */
		}
		for (uint8_t i = 1U; i < code; i++) {
			if ((read >= length) || (write >= output_size)) {
				return 0U;
			}
			output[write] = input[read];
			write++;
			read++;
		}
		if ((code != 0xFFU) && (read < length)) {
			if (write >= output_size) {
				return 0U;
			}
			output[write] = 0U;
			write++;
		}
	}

	return write;
}

/* ------------------------------------------------------------------------- */
/* Encoding                                                                  */
/* ------------------------------------------------------------------------- */

static size_t FinishRecord(uint8_t *record, size_t payload_length,
		uint8_t *out, size_t out_size) {
	record[payload_length] = CanStreamCrc8(record, payload_length);
	return CanStreamCobsEncode(record, payload_length + 1U, out, out_size);
}

size_t CanStreamEncodeFrame(const CanStreamFrame *frame, uint8_t *out,
		size_t out_size) {
	uint8_t record[CAN_STREAM_LEN_FRAME_BASE + 8U];
	size_t offset = 0U;
	uint16_t idc;

	if ((frame == NULL) || (out == NULL) || (frame->dlc > 8U)
			|| (frame->id > 0x7FFU) || (frame->channel < 1U)
			|| (frame->channel > 2U)) {
		return 0U;
	}

	idc = (uint16_t) (frame->id | ((uint16_t) frame->dlc << 11)
			| ((uint16_t) (frame->channel - 1U) << 15));

	/*
	 * Every record type keeps the same three byte header: type followed by the
	 * sequence number. A receiver can therefore detect gaps in the stream
	 * without knowing how to interpret the record body.
	 */
	PutU8(record, &offset, (uint8_t) CAN_STREAM_REC_FRAME);
	PutU16(record, &offset, frame->seq);
	PutU16(record, &offset, idc);
	PutU32(record, &offset, frame->ts32);
	for (uint8_t i = 0U; i < frame->dlc; i++) {
		PutU8(record, &offset, frame->data[i]);
	}

	return FinishRecord(record, offset, out, out_size);
}

size_t CanStreamEncodeTimeSync(uint16_t seq, uint64_t ts64, uint8_t *out,
		size_t out_size) {
	uint8_t record[CAN_STREAM_LEN_TIME_SYNC];
	size_t offset = 0U;

	if (out == NULL) {
		return 0U;
	}
	PutU8(record, &offset, (uint8_t) CAN_STREAM_REC_TIME_SYNC);
	PutU16(record, &offset, seq);
	PutU64(record, &offset, ts64);

	return FinishRecord(record, offset, out, out_size);
}

size_t CanStreamEncodeSession(uint16_t seq, const CanStreamSession *session,
		uint8_t *out, size_t out_size) {
	uint8_t record[CAN_STREAM_LEN_SESSION];
	size_t offset = 0U;

	if ((session == NULL) || (out == NULL)) {
		return 0U;
	}
	PutU8(record, &offset, (uint8_t) CAN_STREAM_REC_SESSION);
	PutU16(record, &offset, seq);
	PutU8(record, &offset, session->proto_version);
	PutU8(record, &offset, session->flags);
	PutU16(record, &offset, session->max_record_len);
	PutU64(record, &offset, session->ts64);
	PutU32(record, &offset, session->can1_bitrate);
	PutU32(record, &offset, session->can2_bitrate);
	PutU32(record, &offset, session->can1_btr);
	PutU32(record, &offset, session->can2_btr);
	PutU32(record, &offset, session->uart_baudrate);
	PutU8(record, &offset, session->rtc_valid);
	PutU8(record, &offset, session->rtc_year);
	PutU8(record, &offset, session->rtc_month);
	PutU8(record, &offset, session->rtc_day);
	PutU8(record, &offset, session->rtc_hour);
	PutU8(record, &offset, session->rtc_minute);
	PutU8(record, &offset, session->rtc_second);

	return FinishRecord(record, offset, out, out_size);
}

size_t CanStreamEncodeEvent(uint16_t seq, const CanStreamEvent *event,
		uint8_t *out, size_t out_size) {
	uint8_t record[CAN_STREAM_LEN_EVENT];
	size_t offset = 0U;

	if ((event == NULL) || (out == NULL)) {
		return 0U;
	}
	PutU8(record, &offset, (uint8_t) CAN_STREAM_REC_EVENT);
	PutU16(record, &offset, seq);
	PutU8(record, &offset, event->channel);
	PutU8(record, &offset, event->reason);
	PutU8(record, &offset, event->state);
	PutU16(record, &offset, event->frame_id);
	PutU32(record, &offset, event->error_code);
	PutU32(record, &offset, event->suppressed);
	PutU64(record, &offset, event->ts64);

	return FinishRecord(record, offset, out, out_size);
}

size_t CanStreamEncodeBusStats(uint16_t seq, const CanStreamBusStats *stats,
		uint8_t *out, size_t out_size) {
	uint8_t record[CAN_STREAM_LEN_BUS_STATS];
	size_t offset = 0U;

	if ((stats == NULL) || (out == NULL)) {
		return 0U;
	}
	PutU8(record, &offset, (uint8_t) CAN_STREAM_REC_BUS_STATS);
	PutU16(record, &offset, seq);
	PutU8(record, &offset, stats->channel);
	PutU64(record, &offset, stats->ts64);
	PutU32(record, &offset, stats->rx_frames);
	PutU32(record, &offset, stats->valid_frames);
	PutU32(record, &offset, stats->buffered_frames);
	PutU32(record, &offset, stats->payload_bytes);
	PutU32(record, &offset, stats->rx_read_errors);
	PutU32(record, &offset, stats->extended_frames);
	PutU32(record, &offset, stats->remote_frames);
	PutU32(record, &offset, stats->invalid_dlc);
	PutU32(record, &offset, stats->fifo_full);
	PutU32(record, &offset, stats->fifo_overrun);
	PutU32(record, &offset, stats->ring_dropped);
	PutU32(record, &offset, stats->warning_entries);
	PutU32(record, &offset, stats->passive_entries);
	PutU32(record, &offset, stats->bus_off_entries);
	PutU32(record, &offset, stats->stuff_errors);
	PutU32(record, &offset, stats->form_errors);
	PutU32(record, &offset, stats->ack_errors);
	PutU32(record, &offset, stats->bit_recessive_errors);
	PutU32(record, &offset, stats->bit_dominant_errors);
	PutU32(record, &offset, stats->crc_errors);
	PutU32(record, &offset, stats->other_errors);
	PutU32(record, &offset, stats->last_error_code);
	PutU32(record, &offset, stats->last_ring_drop_id);
	PutU8(record, &offset, stats->fifo_max_fill);
	PutU8(record, &offset, stats->current_rec);
	PutU8(record, &offset, stats->current_tec);
	PutU8(record, &offset, stats->max_rec);
	PutU8(record, &offset, stats->max_tec);
	PutU8(record, &offset, stats->state);

	return FinishRecord(record, offset, out, out_size);
}

size_t CanStreamEncodeLoggerStats(uint16_t seq,
		const CanStreamLoggerStats *stats, uint8_t *out, size_t out_size) {
	uint8_t record[CAN_STREAM_LEN_LOGGER_STATS];
	size_t offset = 0U;

	if ((stats == NULL) || (out == NULL)) {
		return 0U;
	}
	PutU8(record, &offset, (uint8_t) CAN_STREAM_REC_LOGGER_STATS);
	PutU16(record, &offset, seq);
	PutU64(record, &offset, stats->ts64);
	PutU32(record, &offset, stats->ring_drop_total);
	PutU32(record, &offset, stats->tx_records_sent);
	PutU32(record, &offset, stats->tx_bytes_sent);
	PutU32(record, &offset, stats->tx_records_dropped);
	PutU32(record, &offset, stats->tx_bytes_dropped);
	PutU32(record, &offset, stats->event_queue_dropped);
	PutU32(record, &offset, stats->loop_max_us);
	PutU16(record, &offset, stats->ring_peak);
	PutU16(record, &offset, stats->ring_capacity);
	PutU16(record, &offset, stats->tx_buffer_peak);
	PutU16(record, &offset, stats->tx_buffer_capacity);
	PutU8(record, &offset, stats->flags);

	return FinishRecord(record, offset, out, out_size);
}

size_t CanStreamEncodeAck(uint16_t seq, const CanStreamAck *ack, uint8_t *out,
		size_t out_size) {
	uint8_t record[CAN_STREAM_LEN_ACK];
	size_t offset = 0U;

	if ((ack == NULL) || (out == NULL)) {
		return 0U;
	}
	PutU8(record, &offset, (uint8_t) CAN_STREAM_REC_ACK);
	PutU16(record, &offset, seq);
	PutU8(record, &offset, ack->status);
	PutU8(record, &offset, ack->command);
	PutU32(record, &offset, ack->value);
	PutU64(record, &offset, ack->ts64);

	return FinishRecord(record, offset, out, out_size);
}

/* ------------------------------------------------------------------------- */
/* Decoding                                                                  */
/* ------------------------------------------------------------------------- */

void CanStreamDecoderInit(CanStreamDecoder *decoder) {
	if (decoder != NULL) {
		memset(decoder, 0, sizeof(*decoder));
	}
}

uint8_t CanStreamDecoderFeed(CanStreamDecoder *decoder, uint8_t byte,
		CanStreamRecord *record) {
	uint8_t decoded[CAN_STREAM_MAX_RECORD];
	size_t decoded_length;

	if ((decoder == NULL) || (record == NULL)) {
		return 0U;
	}

	if (byte != 0x00U) {
		if (decoder->length < (uint16_t) sizeof(decoder->buffer)) {
			decoder->buffer[decoder->length] = byte;
			decoder->length++;
		} else {
			decoder->overflow = 1U;
		}
		return 0U;
	}

	/* Delimiter reached. */
	if (decoder->overflow != 0U) {
		decoder->sync_losses++;
		decoder->length = 0U;
		decoder->overflow = 0U;
		return 0U;
	}
	if (decoder->length == 0U) {
		return 0U; /* idle line or back to back delimiters */
	}

	decoded_length = CanStreamCobsDecode(decoder->buffer, decoder->length,
			decoded, sizeof(decoded));
	decoder->length = 0U;

	if (decoded_length == 0U) {
		decoder->cobs_errors++;
		return 0U;
	}
	if (decoded_length < CAN_STREAM_MIN_RECORD) {
		decoder->short_records++;
		return 0U;
	}
	if (CanStreamCrc8(decoded, decoded_length - 1U)
			!= decoded[decoded_length - 1U]) {
		decoder->crc_errors++;
		return 0U;
	}

	memcpy(record->payload, decoded, decoded_length);
	record->length = (uint16_t) decoded_length;
	record->type = decoded[0];
	record->seq = (uint16_t) ((uint16_t) decoded[1]
			| ((uint16_t) decoded[2] << 8));
	decoder->records_ok++;

	return 1U;
}

uint8_t CanStreamDecodeFrame(const CanStreamRecord *record,
		CanStreamFrame *frame) {
	size_t offset = 1U; /* skip type */
	uint16_t idc;
	uint8_t dlc;

	if ((record == NULL) || (frame == NULL)
			|| (record->type != (uint8_t) CAN_STREAM_REC_FRAME)
			|| (record->length < (CAN_STREAM_LEN_FRAME_BASE))) {
		return 0U;
	}

	frame->seq = GetU16(record->payload, &offset);
	idc = GetU16(record->payload, &offset);
	dlc = (uint8_t) ((idc >> 11) & 0x0FU);
	if (dlc > 8U) {
		return 0U;
	}
	if (record->length != (CAN_STREAM_LEN_FRAME_BASE + dlc)) {
		return 0U;
	}

	frame->id = (uint16_t) (idc & 0x7FFU);
	frame->dlc = dlc;
	frame->channel = (uint8_t) (((idc >> 15) & 0x01U) + 1U);
	frame->ts32 = GetU32(record->payload, &offset);
	memset(frame->data, 0, sizeof(frame->data));
	for (uint8_t i = 0U; i < dlc; i++) {
		frame->data[i] = GetU8(record->payload, &offset);
	}

	return 1U;
}

uint8_t CanStreamDecodeTimeSync(const CanStreamRecord *record,
		uint64_t *ts64) {
	size_t offset = 3U; /* type + seq */

	if ((record == NULL) || (ts64 == NULL)
			|| (record->type != (uint8_t) CAN_STREAM_REC_TIME_SYNC)
			|| (record->length != CAN_STREAM_LEN_TIME_SYNC)) {
		return 0U;
	}
	*ts64 = GetU64(record->payload, &offset);
	return 1U;
}

uint8_t CanStreamDecodeSession(const CanStreamRecord *record,
		CanStreamSession *session) {
	size_t offset = 3U;

	if ((record == NULL) || (session == NULL)
			|| (record->type != (uint8_t) CAN_STREAM_REC_SESSION)
			|| (record->length != CAN_STREAM_LEN_SESSION)) {
		return 0U;
	}
	session->proto_version = GetU8(record->payload, &offset);
	session->flags = GetU8(record->payload, &offset);
	session->max_record_len = GetU16(record->payload, &offset);
	session->ts64 = GetU64(record->payload, &offset);
	session->can1_bitrate = GetU32(record->payload, &offset);
	session->can2_bitrate = GetU32(record->payload, &offset);
	session->can1_btr = GetU32(record->payload, &offset);
	session->can2_btr = GetU32(record->payload, &offset);
	session->uart_baudrate = GetU32(record->payload, &offset);
	session->rtc_valid = GetU8(record->payload, &offset);
	session->rtc_year = GetU8(record->payload, &offset);
	session->rtc_month = GetU8(record->payload, &offset);
	session->rtc_day = GetU8(record->payload, &offset);
	session->rtc_hour = GetU8(record->payload, &offset);
	session->rtc_minute = GetU8(record->payload, &offset);
	session->rtc_second = GetU8(record->payload, &offset);
	return 1U;
}

uint8_t CanStreamDecodeEvent(const CanStreamRecord *record,
		CanStreamEvent *event) {
	size_t offset = 3U;

	if ((record == NULL) || (event == NULL)
			|| (record->type != (uint8_t) CAN_STREAM_REC_EVENT)
			|| (record->length != CAN_STREAM_LEN_EVENT)) {
		return 0U;
	}
	event->channel = GetU8(record->payload, &offset);
	event->reason = GetU8(record->payload, &offset);
	event->state = GetU8(record->payload, &offset);
	event->frame_id = GetU16(record->payload, &offset);
	event->error_code = GetU32(record->payload, &offset);
	event->suppressed = GetU32(record->payload, &offset);
	event->ts64 = GetU64(record->payload, &offset);
	return 1U;
}

uint8_t CanStreamDecodeBusStats(const CanStreamRecord *record,
		CanStreamBusStats *stats) {
	size_t offset = 3U;

	if ((record == NULL) || (stats == NULL)
			|| (record->type != (uint8_t) CAN_STREAM_REC_BUS_STATS)
			|| (record->length != CAN_STREAM_LEN_BUS_STATS)) {
		return 0U;
	}
	stats->channel = GetU8(record->payload, &offset);
	stats->ts64 = GetU64(record->payload, &offset);
	stats->rx_frames = GetU32(record->payload, &offset);
	stats->valid_frames = GetU32(record->payload, &offset);
	stats->buffered_frames = GetU32(record->payload, &offset);
	stats->payload_bytes = GetU32(record->payload, &offset);
	stats->rx_read_errors = GetU32(record->payload, &offset);
	stats->extended_frames = GetU32(record->payload, &offset);
	stats->remote_frames = GetU32(record->payload, &offset);
	stats->invalid_dlc = GetU32(record->payload, &offset);
	stats->fifo_full = GetU32(record->payload, &offset);
	stats->fifo_overrun = GetU32(record->payload, &offset);
	stats->ring_dropped = GetU32(record->payload, &offset);
	stats->warning_entries = GetU32(record->payload, &offset);
	stats->passive_entries = GetU32(record->payload, &offset);
	stats->bus_off_entries = GetU32(record->payload, &offset);
	stats->stuff_errors = GetU32(record->payload, &offset);
	stats->form_errors = GetU32(record->payload, &offset);
	stats->ack_errors = GetU32(record->payload, &offset);
	stats->bit_recessive_errors = GetU32(record->payload, &offset);
	stats->bit_dominant_errors = GetU32(record->payload, &offset);
	stats->crc_errors = GetU32(record->payload, &offset);
	stats->other_errors = GetU32(record->payload, &offset);
	stats->last_error_code = GetU32(record->payload, &offset);
	stats->last_ring_drop_id = GetU32(record->payload, &offset);
	stats->fifo_max_fill = GetU8(record->payload, &offset);
	stats->current_rec = GetU8(record->payload, &offset);
	stats->current_tec = GetU8(record->payload, &offset);
	stats->max_rec = GetU8(record->payload, &offset);
	stats->max_tec = GetU8(record->payload, &offset);
	stats->state = GetU8(record->payload, &offset);
	return 1U;
}

uint8_t CanStreamDecodeLoggerStats(const CanStreamRecord *record,
		CanStreamLoggerStats *stats) {
	size_t offset = 3U;

	if ((record == NULL) || (stats == NULL)
			|| (record->type != (uint8_t) CAN_STREAM_REC_LOGGER_STATS)
			|| (record->length != CAN_STREAM_LEN_LOGGER_STATS)) {
		return 0U;
	}
	stats->ts64 = GetU64(record->payload, &offset);
	stats->ring_drop_total = GetU32(record->payload, &offset);
	stats->tx_records_sent = GetU32(record->payload, &offset);
	stats->tx_bytes_sent = GetU32(record->payload, &offset);
	stats->tx_records_dropped = GetU32(record->payload, &offset);
	stats->tx_bytes_dropped = GetU32(record->payload, &offset);
	stats->event_queue_dropped = GetU32(record->payload, &offset);
	stats->loop_max_us = GetU32(record->payload, &offset);
	stats->ring_peak = GetU16(record->payload, &offset);
	stats->ring_capacity = GetU16(record->payload, &offset);
	stats->tx_buffer_peak = GetU16(record->payload, &offset);
	stats->tx_buffer_capacity = GetU16(record->payload, &offset);
	stats->flags = GetU8(record->payload, &offset);
	return 1U;
}

uint8_t CanStreamDecodeAck(const CanStreamRecord *record, CanStreamAck *ack) {
	size_t offset = 3U;

	if ((record == NULL) || (ack == NULL)
			|| (record->type != (uint8_t) CAN_STREAM_REC_ACK)
			|| (record->length != CAN_STREAM_LEN_ACK)) {
		return 0U;
	}
	ack->status = GetU8(record->payload, &offset);
	ack->command = GetU8(record->payload, &offset);
	ack->value = GetU32(record->payload, &offset);
	ack->ts64 = GetU64(record->payload, &offset);
	return 1U;
}

uint64_t CanStreamRestoreTime(uint64_t sync_ts64, uint32_t frame_ts32) {
	uint32_t sync32 = (uint32_t) sync_ts64;
	int32_t delta = (int32_t) (frame_ts32 - sync32);

	return (uint64_t) ((int64_t) sync_ts64 + (int64_t) delta);
}
