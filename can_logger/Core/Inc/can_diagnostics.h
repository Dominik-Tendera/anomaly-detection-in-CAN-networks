/*
 * can_diagnostics.h
 *
 * Lightweight CAN health counters. Interrupt callbacks only update counters;
 * text formatting and USB writes stay in the main application loop.
 */

#ifndef INC_CAN_DIAGNOSTICS_H_
#define INC_CAN_DIAGNOSTICS_H_

#include "main.h"

#define CAN_DIAG_BUS_COUNT 2U

#define CAN_DIAG_EVENT_CONTROLLER_ERROR (1UL << 0)
#define CAN_DIAG_EVENT_FIFO_OVERRUN      (1UL << 1)
#define CAN_DIAG_EVENT_STATE_CHANGE      (1UL << 2)
#define CAN_DIAG_EVENT_RING_DROP         (1UL << 3)

typedef enum {
	CAN_DIAG_STATE_ACTIVE = 0,
	CAN_DIAG_STATE_WARNING,
	CAN_DIAG_STATE_PASSIVE,
	CAN_DIAG_STATE_BUS_OFF
} CAN_DiagnosticsState_t;

typedef enum {
	CAN_DIAG_INVALID_EXTENDED = 0,
	CAN_DIAG_INVALID_REMOTE,
	CAN_DIAG_INVALID_DLC
} CAN_DiagnosticsInvalidFrame_t;

typedef struct {
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
	uint64_t last_event_timestamp;
	uint8_t fifo_max_fill;
	uint8_t current_rec;
	uint8_t current_tec;
	uint8_t max_rec;
	uint8_t max_tec;
	CAN_DiagnosticsState_t state;
} CAN_DiagnosticsSnapshot_t;

typedef struct {
	uint32_t flags;
	uint32_t last_error_code;
	uint64_t timestamp;
} CAN_DiagnosticsEvent_t;

void CAN_DiagnosticsInit(void);
void CAN_DiagnosticsObserveController(uint8_t bus_number, uint32_t esr,
		uint64_t timestamp);
void CAN_DiagnosticsRecordFifoLevel(uint8_t bus_number, uint32_t fill_level);
void CAN_DiagnosticsRecordFifoFull(uint8_t bus_number);
void CAN_DiagnosticsRecordRxReadError(uint8_t bus_number);
void CAN_DiagnosticsRecordRxFrame(uint8_t bus_number);
void CAN_DiagnosticsRecordValidFrame(uint8_t bus_number, uint32_t dlc);
void CAN_DiagnosticsRecordInvalidFrame(uint8_t bus_number,
		CAN_DiagnosticsInvalidFrame_t reason);
void CAN_DiagnosticsRecordBufferedFrame(uint8_t bus_number);
void CAN_DiagnosticsRecordRingDrop(uint8_t bus_number, uint32_t frame_id,
		uint64_t timestamp);
void CAN_DiagnosticsRecordError(uint8_t bus_number, uint32_t error_code,
		uint32_t esr, uint64_t timestamp);
uint8_t CAN_DiagnosticsGetSnapshot(uint8_t bus_number,
		CAN_DiagnosticsSnapshot_t *snapshot);
uint8_t CAN_DiagnosticsTakeEvent(uint8_t bus_number,
		CAN_DiagnosticsEvent_t *event);

#endif /* INC_CAN_DIAGNOSTICS_H_ */
