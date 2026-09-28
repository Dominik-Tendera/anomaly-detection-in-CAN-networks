/*
 * can_stream_codec.h
 *
 * Binary record format for the CAN logger to Raspberry Pi link.
 *
 * This translation unit is deliberately free of any HAL or STM32 dependency so
 * that the very same source file can be compiled on a host and exercised by
 * round-trip tests. The firmware transport lives in can_stream.c.
 *
 * Wire format: every record is COBS encoded and terminated by a single 0x00
 * delimiter, so a 0x00 byte never appears inside an encoded record and the
 * decoder can always resynchronise on the next delimiter. Multi-byte fields are
 * little endian. The trailing byte of a decoded record is a CRC-8 computed over
 * all preceding bytes of that record.
 */

#ifndef INC_CAN_STREAM_CODEC_H_
#define INC_CAN_STREAM_CODEC_H_

#include <stddef.h>
#include <stdint.h>

#define CAN_STREAM_PROTO_VERSION 1U

/* Longest decoded record is CAN_STREAM_REC_BUS_STATS (111 bytes). */
#define CAN_STREAM_MAX_RECORD 128U

/*
 * COBS overhead for n <= 254 payload bytes is one code byte, plus the
 * delimiter. One spare byte keeps the bound comfortable.
 */
#define CAN_STREAM_MAX_ENCODED (CAN_STREAM_MAX_RECORD + 3U)

#define CAN_STREAM_FRAME_ID_UNKNOWN 0xFFFFU

/* Decoded record lengths, including the type byte and the trailing CRC. */
#define CAN_STREAM_LEN_FRAME_BASE 10U /* plus DLC payload bytes */
#define CAN_STREAM_LEN_TIME_SYNC 12U
#define CAN_STREAM_LEN_SESSION 43U
#define CAN_STREAM_LEN_EVENT 25U
#define CAN_STREAM_LEN_BUS_STATS 111U
#define CAN_STREAM_LEN_LOGGER_STATS 49U
#define CAN_STREAM_LEN_ACK 18U

/* Shortest possible record: type, sequence number and CRC. */
#define CAN_STREAM_MIN_RECORD 4U

/* COBS adds exactly one code byte and one delimiter for records this short. */
#define CAN_STREAM_COBS_OVERHEAD 2U

typedef enum {
	CAN_STREAM_REC_FRAME = 0x01,
	CAN_STREAM_REC_TIME_SYNC = 0x02,
	CAN_STREAM_REC_SESSION = 0x03,
	CAN_STREAM_REC_EVENT = 0x04,
	CAN_STREAM_REC_BUS_STATS = 0x05,
	CAN_STREAM_REC_LOGGER_STATS = 0x06,
	CAN_STREAM_REC_ACK = 0x07,
	/*
	 * Reserved for the future record carrying CANH, CANL, V_diff and V_CM
	 * aggregates. Decoders must skip unknown types instead of failing, so
	 * adding it later does not break an existing receiver.
	 */
	CAN_STREAM_REC_ANALOG_AGG = 0x20
} CanStreamRecordType;

typedef enum {
	CAN_STREAM_EVENT_RX_READ_ERROR = 0,
	CAN_STREAM_EVENT_EXTENDED_REJECTED = 1,
	CAN_STREAM_EVENT_REMOTE_REJECTED = 2,
	CAN_STREAM_EVENT_INVALID_DLC = 3,
	CAN_STREAM_EVENT_RING_DROP = 4,
	CAN_STREAM_EVENT_FIFO_FULL = 5,
	CAN_STREAM_EVENT_FIFO_OVERRUN = 6,
	CAN_STREAM_EVENT_CONTROLLER_ERROR = 7,
	CAN_STREAM_EVENT_STATE_CHANGE = 8,
	CAN_STREAM_EVENT_TX_OVERFLOW = 9,
	CAN_STREAM_EVENT_USB_LOG_BOUNDARY = 10,
	/*
	 * A frame arrived with a 29-bit identifier whose value still fits in 11 bits
	 * and was carried as a standard frame. Reported once per session, the running
	 * count lives in the extended_frames counter of the bus statistics.
	 */
	CAN_STREAM_EVENT_EXTENDED_REMAPPED = 11,
	CAN_STREAM_EVENT_REASON_COUNT = 12
} CanStreamEventReason;

#define CAN_STREAM_FLAG_MEASUREMENT_MODE (1U << 0)
#define CAN_STREAM_FLAG_USB_LOGGING      (1U << 1)
#define CAN_STREAM_FLAG_STREAM_ENABLED   (1U << 2)

typedef struct {
	uint32_t ts32; /* low 32 bits of the device microsecond counter */
	uint16_t seq;
	uint16_t id; /* 11-bit standard identifier, 0..2047 */
	uint8_t channel; /* 1 or 2 */
	uint8_t dlc; /* 0..8 */
	uint8_t data[8];
} CanStreamFrame;

typedef struct {
	uint64_t ts64;
	uint32_t can1_bitrate;
	uint32_t can2_bitrate;
	uint32_t can1_btr;
	uint32_t can2_btr;
	uint32_t uart_baudrate;
	uint16_t max_record_len;
	uint8_t proto_version;
	uint8_t flags;
	uint8_t rtc_valid;
	uint8_t rtc_year;
	uint8_t rtc_month;
	uint8_t rtc_day;
	uint8_t rtc_hour;
	uint8_t rtc_minute;
	uint8_t rtc_second;
} CanStreamSession;

typedef struct {
	uint64_t ts64;
	uint32_t error_code;
	uint32_t suppressed; /* events of this class dropped by rate limiting */
	uint16_t frame_id; /* CAN_STREAM_FRAME_ID_UNKNOWN when not applicable */
	uint8_t channel;
	uint8_t reason;
	uint8_t state;
} CanStreamEvent;

typedef struct {
	uint64_t ts64;
	uint32_t rx_frames;
	uint32_t valid_frames;
	uint32_t buffered_frames;
	uint32_t payload_bytes;
	uint32_t rx_read_errors;
	uint32_t extended_frames;
	uint32_t remote_frames;
	uint32_t invalid_dlc;
	uint32_t fifo_full;
	uint32_t fifo_overrun;
	uint32_t ring_dropped;
	uint32_t warning_entries;
	uint32_t passive_entries;
	uint32_t bus_off_entries;
	uint32_t stuff_errors;
	uint32_t form_errors;
	uint32_t ack_errors;
	uint32_t bit_recessive_errors;
	uint32_t bit_dominant_errors;
	uint32_t crc_errors;
	uint32_t other_errors;
	uint32_t last_error_code;
	uint32_t last_ring_drop_id;
	uint8_t channel;
	uint8_t fifo_max_fill;
	uint8_t current_rec;
	uint8_t current_tec;
	uint8_t max_rec;
	uint8_t max_tec;
	uint8_t state;
} CanStreamBusStats;

typedef struct {
	uint64_t ts64;
	uint32_t ring_drop_total;
	uint32_t tx_records_sent;
	uint32_t tx_bytes_sent;
	uint32_t tx_records_dropped;
	uint32_t tx_bytes_dropped;
	uint32_t event_queue_dropped;
	uint32_t loop_max_us;
	uint16_t ring_peak;
	uint16_t ring_capacity;
	uint16_t tx_buffer_peak;
	uint16_t tx_buffer_capacity;
	uint8_t flags;
} CanStreamLoggerStats;

typedef struct {
	uint64_t ts64;
	uint32_t value;
	uint8_t status; /* 0 accepted, 1 rejected */
	uint8_t command;
} CanStreamAck;

/* ------------------------------------------------------------------------- */
/* Primitives                                                                */
/* ------------------------------------------------------------------------- */

uint8_t CanStreamCrc8(const uint8_t *data, size_t length);

/*
 * COBS. CanStreamCobsEncode writes the encoded bytes followed by the 0x00
 * delimiter and returns the total number of bytes written, or 0 when the
 * output buffer is too small.
 */
size_t CanStreamCobsEncode(const uint8_t *input, size_t length, uint8_t *output,
		size_t output_size);
size_t CanStreamCobsDecode(const uint8_t *input, size_t length, uint8_t *output,
		size_t output_size);

/* ------------------------------------------------------------------------- */
/* Encoders. Each returns the number of bytes written to out, 0 on failure.   */
/* The returned buffer already contains the trailing 0x00 delimiter.          */
/* ------------------------------------------------------------------------- */

size_t CanStreamEncodeFrame(const CanStreamFrame *frame, uint8_t *out,
		size_t out_size);
size_t CanStreamEncodeTimeSync(uint16_t seq, uint64_t ts64, uint8_t *out,
		size_t out_size);
size_t CanStreamEncodeSession(uint16_t seq, const CanStreamSession *session,
		uint8_t *out, size_t out_size);
size_t CanStreamEncodeEvent(uint16_t seq, const CanStreamEvent *event,
		uint8_t *out, size_t out_size);
size_t CanStreamEncodeBusStats(uint16_t seq, const CanStreamBusStats *stats,
		uint8_t *out, size_t out_size);
size_t CanStreamEncodeLoggerStats(uint16_t seq,
		const CanStreamLoggerStats *stats, uint8_t *out, size_t out_size);
size_t CanStreamEncodeAck(uint16_t seq, const CanStreamAck *ack, uint8_t *out,
		size_t out_size);

/* ------------------------------------------------------------------------- */
/* Decoder                                                                   */
/* ------------------------------------------------------------------------- */

typedef struct {
	uint8_t type;
	uint16_t seq;
	uint16_t length; /* decoded record length, including type and CRC */
	uint8_t payload[CAN_STREAM_MAX_RECORD];
} CanStreamRecord;

typedef struct {
	uint8_t buffer[CAN_STREAM_MAX_ENCODED];
	uint16_t length;
	uint8_t overflow; /* current run exceeded the record bound */
	uint32_t records_ok;
	uint32_t crc_errors;
	uint32_t cobs_errors;
	uint32_t sync_losses;
	uint32_t short_records;
} CanStreamDecoder;

void CanStreamDecoderInit(CanStreamDecoder *decoder);

/*
 * Feeds one byte. Returns 1 and fills record when a CRC-valid record has been
 * completed, otherwise 0. Errors update the decoder counters and never abort
 * the stream: the decoder always resumes at the next delimiter.
 */
uint8_t CanStreamDecoderFeed(CanStreamDecoder *decoder, uint8_t byte,
		CanStreamRecord *record);

/* Field extraction. Each returns 1 on success, 0 on a malformed record. */
uint8_t CanStreamDecodeFrame(const CanStreamRecord *record,
		CanStreamFrame *frame);
uint8_t CanStreamDecodeTimeSync(const CanStreamRecord *record, uint64_t *ts64);
uint8_t CanStreamDecodeSession(const CanStreamRecord *record,
		CanStreamSession *session);
uint8_t CanStreamDecodeEvent(const CanStreamRecord *record,
		CanStreamEvent *event);
uint8_t CanStreamDecodeBusStats(const CanStreamRecord *record,
		CanStreamBusStats *stats);
uint8_t CanStreamDecodeLoggerStats(const CanStreamRecord *record,
		CanStreamLoggerStats *stats);
uint8_t CanStreamDecodeAck(const CanStreamRecord *record, CanStreamAck *ack);

/*
 * Rebuilds the full 64-bit device time of a frame from its 32-bit field and the
 * newest time synchronisation record. The 32-bit difference is interpreted as
 * signed, so frames stamped slightly before the synchronisation record are
 * reconstructed correctly as well.
 */
uint64_t CanStreamRestoreTime(uint64_t sync_ts64, uint32_t frame_ts32);

#endif /* INC_CAN_STREAM_CODEC_H_ */
