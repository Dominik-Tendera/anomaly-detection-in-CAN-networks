#include "can_diagnostics.h"

#include <string.h>

typedef struct {
	CAN_DiagnosticsSnapshot_t counters;
	uint32_t pending_events;
} CAN_DiagnosticsBus_t;

static CAN_DiagnosticsBus_t diagnostics[CAN_DIAG_BUS_COUNT];

static uint32_t CAN_DiagnosticsEnterCritical(void) {
	uint32_t primask = __get_PRIMASK();
	__disable_irq();
	return primask;
}

static void CAN_DiagnosticsExitCritical(uint32_t primask) {
	if (primask == 0U) {
		__enable_irq();
	}
}

static CAN_DiagnosticsBus_t *CAN_DiagnosticsGetBus(uint8_t bus_number) {
	if ((bus_number == 0U) || (bus_number > CAN_DIAG_BUS_COUNT)) {
		return NULL;
	}
	return &diagnostics[bus_number - 1U];
}

static CAN_DiagnosticsState_t CAN_DiagnosticsStateFromEsr(uint32_t esr) {
	if ((esr & CAN_ESR_BOFF) != 0U) {
		return CAN_DIAG_STATE_BUS_OFF;
	}
	if ((esr & CAN_ESR_EPVF) != 0U) {
		return CAN_DIAG_STATE_PASSIVE;
	}
	if ((esr & CAN_ESR_EWGF) != 0U) {
		return CAN_DIAG_STATE_WARNING;
	}
	return CAN_DIAG_STATE_ACTIVE;
}

static void CAN_DiagnosticsSetEvent(CAN_DiagnosticsBus_t *bus,
		uint32_t flags, uint64_t timestamp) {
	bus->pending_events |= flags;
	bus->counters.last_event_timestamp = timestamp;
}

void CAN_DiagnosticsInit(void) {
	uint32_t primask = CAN_DiagnosticsEnterCritical();
	memset(diagnostics, 0, sizeof(diagnostics));
	CAN_DiagnosticsExitCritical(primask);
}

void CAN_DiagnosticsObserveController(uint8_t bus_number, uint32_t esr,
		uint64_t timestamp) {
	CAN_DiagnosticsBus_t *bus = CAN_DiagnosticsGetBus(bus_number);
	CAN_DiagnosticsState_t state;
	uint8_t rec;
	uint8_t tec;
	uint32_t primask;

	if (bus == NULL) {
		return;
	}

	state = CAN_DiagnosticsStateFromEsr(esr);
	rec = (uint8_t) ((esr & CAN_ESR_REC) >> CAN_ESR_REC_Pos);
	tec = (uint8_t) ((esr & CAN_ESR_TEC) >> CAN_ESR_TEC_Pos);

	primask = CAN_DiagnosticsEnterCritical();
	bus->counters.current_rec = rec;
	bus->counters.current_tec = tec;
	if (rec > bus->counters.max_rec) {
		bus->counters.max_rec = rec;
	}
	if (tec > bus->counters.max_tec) {
		bus->counters.max_tec = tec;
	}
	if (state != bus->counters.state) {
		bus->counters.state = state;
		if (state == CAN_DIAG_STATE_WARNING) {
			bus->counters.warning_entries++;
		} else if (state == CAN_DIAG_STATE_PASSIVE) {
			bus->counters.passive_entries++;
		} else if (state == CAN_DIAG_STATE_BUS_OFF) {
			bus->counters.bus_off_entries++;
		}
		CAN_DiagnosticsSetEvent(bus, CAN_DIAG_EVENT_STATE_CHANGE, timestamp);
	}
	CAN_DiagnosticsExitCritical(primask);
}

void CAN_DiagnosticsRecordFifoLevel(uint8_t bus_number, uint32_t fill_level) {
	CAN_DiagnosticsBus_t *bus = CAN_DiagnosticsGetBus(bus_number);
	uint8_t fill;

	if (bus == NULL) {
		return;
	}
	fill = (fill_level > UINT8_MAX) ? UINT8_MAX : (uint8_t) fill_level;
	if (fill > bus->counters.fifo_max_fill) {
		bus->counters.fifo_max_fill = fill;
	}
}

void CAN_DiagnosticsRecordFifoFull(uint8_t bus_number) {
	CAN_DiagnosticsBus_t *bus = CAN_DiagnosticsGetBus(bus_number);

	if (bus != NULL) {
		bus->counters.fifo_full++;
		CAN_DiagnosticsRecordFifoLevel(bus_number, 3U);
	}
}

void CAN_DiagnosticsRecordRxReadError(uint8_t bus_number) {
	CAN_DiagnosticsBus_t *bus = CAN_DiagnosticsGetBus(bus_number);
	if (bus != NULL) {
		bus->counters.rx_read_errors++;
	}
}

void CAN_DiagnosticsRecordRxFrame(uint8_t bus_number) {
	CAN_DiagnosticsBus_t *bus = CAN_DiagnosticsGetBus(bus_number);
	if (bus != NULL) {
		bus->counters.rx_frames++;
	}
}

void CAN_DiagnosticsRecordValidFrame(uint8_t bus_number, uint32_t dlc) {
	CAN_DiagnosticsBus_t *bus = CAN_DiagnosticsGetBus(bus_number);
	if (bus != NULL) {
		bus->counters.valid_frames++;
		bus->counters.payload_bytes += dlc;
	}
}

void CAN_DiagnosticsRecordInvalidFrame(uint8_t bus_number,
		CAN_DiagnosticsInvalidFrame_t reason) {
	CAN_DiagnosticsBus_t *bus = CAN_DiagnosticsGetBus(bus_number);

	if (bus == NULL) {
		return;
	}
	if (reason == CAN_DIAG_INVALID_EXTENDED) {
		bus->counters.extended_frames++;
	} else if (reason == CAN_DIAG_INVALID_REMOTE) {
		bus->counters.remote_frames++;
	} else if (reason == CAN_DIAG_INVALID_DLC) {
		bus->counters.invalid_dlc++;
	}
}

void CAN_DiagnosticsRecordBufferedFrame(uint8_t bus_number) {
	CAN_DiagnosticsBus_t *bus = CAN_DiagnosticsGetBus(bus_number);
	if (bus != NULL) {
		bus->counters.buffered_frames++;
	}
}

void CAN_DiagnosticsRecordRingDrop(uint8_t bus_number, uint32_t frame_id,
		uint64_t timestamp) {
	CAN_DiagnosticsBus_t *bus = CAN_DiagnosticsGetBus(bus_number);
	if (bus != NULL) {
		bus->counters.ring_dropped++;
		bus->counters.last_ring_drop_id = frame_id;
		CAN_DiagnosticsSetEvent(bus, CAN_DIAG_EVENT_RING_DROP, timestamp);
	}
}

void CAN_DiagnosticsRecordError(uint8_t bus_number, uint32_t error_code,
		uint32_t esr, uint64_t timestamp) {
	CAN_DiagnosticsBus_t *bus = CAN_DiagnosticsGetBus(bus_number);
	uint32_t known_errors = HAL_CAN_ERROR_EWG | HAL_CAN_ERROR_EPV
			| HAL_CAN_ERROR_BOF | HAL_CAN_ERROR_STF | HAL_CAN_ERROR_FOR
			| HAL_CAN_ERROR_ACK | HAL_CAN_ERROR_BR | HAL_CAN_ERROR_BD
			| HAL_CAN_ERROR_CRC | HAL_CAN_ERROR_RX_FOV0
			| HAL_CAN_ERROR_RX_FOV1;

	if ((bus == NULL) || (error_code == HAL_CAN_ERROR_NONE)) {
		return;
	}

	bus->counters.last_error_code = error_code;
	if ((error_code & HAL_CAN_ERROR_STF) != 0U) {
		bus->counters.stuff_errors++;
	}
	if ((error_code & HAL_CAN_ERROR_FOR) != 0U) {
		bus->counters.form_errors++;
	}
	if ((error_code & HAL_CAN_ERROR_ACK) != 0U) {
		bus->counters.ack_errors++;
	}
	if ((error_code & HAL_CAN_ERROR_BR) != 0U) {
		bus->counters.bit_recessive_errors++;
	}
	if ((error_code & HAL_CAN_ERROR_BD) != 0U) {
		bus->counters.bit_dominant_errors++;
	}
	if ((error_code & HAL_CAN_ERROR_CRC) != 0U) {
		bus->counters.crc_errors++;
	}
	if ((error_code & (HAL_CAN_ERROR_RX_FOV0 | HAL_CAN_ERROR_RX_FOV1))
			!= 0U) {
		bus->counters.fifo_overrun++;
		CAN_DiagnosticsSetEvent(bus, CAN_DIAG_EVENT_FIFO_OVERRUN,
				timestamp);
	}
	if ((error_code & ~known_errors) != 0U) {
		bus->counters.other_errors++;
	}
	if ((error_code & (HAL_CAN_ERROR_EWG | HAL_CAN_ERROR_EPV
			| HAL_CAN_ERROR_BOF | HAL_CAN_ERROR_STF | HAL_CAN_ERROR_FOR
			| HAL_CAN_ERROR_ACK | HAL_CAN_ERROR_BR | HAL_CAN_ERROR_BD
			| HAL_CAN_ERROR_CRC)) != 0U) {
		CAN_DiagnosticsSetEvent(bus, CAN_DIAG_EVENT_CONTROLLER_ERROR,
				timestamp);
	}

	CAN_DiagnosticsObserveController(bus_number, esr, timestamp);
}

uint8_t CAN_DiagnosticsGetSnapshot(uint8_t bus_number,
		CAN_DiagnosticsSnapshot_t *snapshot) {
	CAN_DiagnosticsBus_t *bus = CAN_DiagnosticsGetBus(bus_number);
	uint32_t primask;

	if ((bus == NULL) || (snapshot == NULL)) {
		return 0U;
	}
	primask = CAN_DiagnosticsEnterCritical();
	*snapshot = bus->counters;
	CAN_DiagnosticsExitCritical(primask);
	return 1U;
}

uint8_t CAN_DiagnosticsTakeEvent(uint8_t bus_number,
		CAN_DiagnosticsEvent_t *event) {
	CAN_DiagnosticsBus_t *bus = CAN_DiagnosticsGetBus(bus_number);
	uint32_t primask;

	if ((bus == NULL) || (event == NULL)) {
		return 0U;
	}
	primask = CAN_DiagnosticsEnterCritical();
	event->flags = bus->pending_events;
	event->last_error_code = bus->counters.last_error_code;
	event->timestamp = bus->counters.last_event_timestamp;
	bus->pending_events = 0U;
	CAN_DiagnosticsExitCritical(primask);
	return (event->flags != 0U) ? 1U : 0U;
}
