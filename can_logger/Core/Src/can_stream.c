/*
 * can_stream.c
 *
 * DMA driven transport of the binary record stream on USART1.
 *
 * Concurrency contract:
 *  - the byte ring has exactly one producer, the main loop, so pushing needs no
 *    critical section;
 *  - the tail is advanced only by the transmit completion interrupt, and both
 *    indices are 16-bit aligned volatiles, so single word accesses are atomic
 *    on this core;
 *  - interrupt handlers never serialise a record. They push a compact entry
 *    into the event queue and the main loop turns it into a record.
 */

#include "can_stream.h"

#include "App.h"
#include "can_diagnostics.h"
#include "rtc.h"
#include "watchdog.h"

#include <stdio.h>
#include <string.h>

extern CAN_HandleTypeDef hcan1;
extern CAN_HandleTypeDef hcan2;
extern UART_HandleTypeDef huart1;

static DMA_HandleTypeDef hdma_usart1_tx;

typedef struct {
	uint64_t timestamp;
	uint32_t error_code;
	uint32_t suppressed;
	uint16_t frame_id;
	uint8_t bus_number;
	uint8_t reason;
	uint8_t state;
} CanStreamQueuedEvent;

typedef struct {
	uint32_t last_tick;
	uint32_t suppressed;
	uint8_t seen;
} CanStreamRateLimit;

static uint8_t tx_buffer[CAN_STREAM_TX_BUFFER_SIZE];
static volatile uint16_t tx_head;
static volatile uint16_t tx_tail;
static volatile uint16_t tx_chunk;
static volatile uint8_t tx_active;

static CanStreamQueuedEvent event_queue[CAN_STREAM_EVENT_QUEUE_SIZE];
static volatile uint8_t event_head;
static volatile uint8_t event_tail;

static CanStreamRateLimit rate_limit[CAN_DIAG_BUS_COUNT][CAN_STREAM_EVENT_REASON_COUNT];

static uint16_t stream_seq;
static uint32_t stream_baudrate = CAN_STREAM_DEFAULT_BAUDRATE;
static uint8_t stream_enabled = 1U;
static uint8_t measurement_mode;
static uint8_t usb_logging;
static uint8_t initialised;

static uint32_t records_sent;
static uint32_t bytes_sent;
static uint32_t records_dropped;
static uint32_t bytes_dropped;
static uint32_t event_queue_dropped;
static uint32_t dma_errors;
static uint32_t frames_rejected;
static uint16_t tx_peak;
static uint32_t loop_max_us;

static uint32_t time_sync_tick;
static uint32_t stats_tick;

/* ------------------------------------------------------------------------- */
/* Small helpers                                                             */
/* ------------------------------------------------------------------------- */

static uint32_t EnterCritical(void) {
	uint32_t primask = __get_PRIMASK();
	__disable_irq();
	return primask;
}

static void ExitCritical(uint32_t primask) {
	if (primask == 0U) {
		__enable_irq();
	}
}

static uint16_t TxUsed(void) {
	uint16_t head = tx_head;
	uint16_t tail = tx_tail;

	if (head >= tail) {
		return (uint16_t) (head - tail);
	}
	return (uint16_t) (CAN_STREAM_TX_BUFFER_SIZE - tail + head);
}

static uint16_t TxFree(void) {
	/* One slot stays empty so that head == tail always means empty. */
	return (uint16_t) (CAN_STREAM_TX_BUFFER_SIZE - 1U - TxUsed());
}

static void CanStreamKick(void) {
	uint32_t primask = EnterCritical();

	if ((tx_active == 0U) && (tx_head != tx_tail)) {
		uint16_t head = tx_head;
		uint16_t tail = tx_tail;
		uint16_t chunk;

		if (head > tail) {
			chunk = (uint16_t) (head - tail);
		} else {
			chunk = (uint16_t) (CAN_STREAM_TX_BUFFER_SIZE - tail);
		}
		if (chunk > CAN_STREAM_DMA_CHUNK_MAX) {
			chunk = CAN_STREAM_DMA_CHUNK_MAX;
		}

		tx_chunk = chunk;
		tx_active = 1U;
		__DMB();
		if (HAL_UART_Transmit_DMA(&huart1, &tx_buffer[tail], chunk)
				!= HAL_OK) {
			tx_active = 0U;
			tx_chunk = 0U;
			dma_errors++;
		}
	}

	ExitCritical(primask);
}

/*
 * Pushes an already encoded record. Returns 1 when the whole record was
 * accepted. A partial record is never written, so the receiver cannot observe a
 * truncated one.
 */
static uint8_t TxPush(const uint8_t *data, uint16_t length) {
	uint16_t head;
	uint16_t used;

	if ((length == 0U) || (length > TxFree())) {
		records_dropped++;
		bytes_dropped += length;
		return 0U;
	}

	head = tx_head;
	for (uint16_t i = 0U; i < length; i++) {
		tx_buffer[head] = data[i];
		head++;
		if (head >= CAN_STREAM_TX_BUFFER_SIZE) {
			head = 0U;
		}
	}
	/* Publish the payload before the index that makes it visible to the ISR. */
	__DMB();
	tx_head = head;

	records_sent++;
	used = TxUsed();
	if (used > tx_peak) {
		tx_peak = used;
	}

	CanStreamKick();
	return 1U;
}

static uint8_t CanStreamBusStateToByte(CAN_DiagnosticsState_t state) {
	return (uint8_t) state;
}

static uint32_t CanStreamBitrate(const CAN_HandleTypeDef *hcan) {
	uint32_t segment1;
	uint32_t segment2;
	uint32_t quanta;

	if ((hcan == NULL) || (hcan->Init.Prescaler == 0U)) {
		return 0U;
	}
	segment1 = ((hcan->Init.TimeSeg1 & CAN_BTR_TS1) >> CAN_BTR_TS1_Pos) + 1U;
	segment2 = ((hcan->Init.TimeSeg2 & CAN_BTR_TS2) >> CAN_BTR_TS2_Pos) + 1U;
	quanta = 1U + segment1 + segment2;

	return HAL_RCC_GetPCLK1Freq() / hcan->Init.Prescaler / quanta;
}

static uint8_t CanStreamFlags(void) {
	uint8_t flags = 0U;

	if (measurement_mode != 0U) {
		flags |= CAN_STREAM_FLAG_MEASUREMENT_MODE;
	}
	if (usb_logging != 0U) {
		flags |= CAN_STREAM_FLAG_USB_LOGGING;
	}
	if (stream_enabled != 0U) {
		flags |= CAN_STREAM_FLAG_STREAM_ENABLED;
	}
	return flags;
}

/* ------------------------------------------------------------------------- */
/* Initialisation                                                            */
/* ------------------------------------------------------------------------- */

static uint8_t CanStreamConfigureUart(uint32_t baudrate) {
	/*
	 * MX_USART1_UART_Init() is generated from the .ioc file and hardcodes
	 * 115200 baud, so the speed is applied here instead of in generated code.
	 * Re-initialising an already initialised handle does not call MspInit
	 * again, therefore the DMA link established below survives the call.
	 */
	huart1.Init.BaudRate = baudrate;
	if (HAL_UART_Init(&huart1) != HAL_OK) {
		return 0U;
	}
	stream_baudrate = baudrate;
	return 1U;
}

static uint8_t CanStreamConfigureDma(void) {
	__HAL_RCC_DMA2_CLK_ENABLE();

	hdma_usart1_tx.Instance = DMA2_Stream7;
	hdma_usart1_tx.Init.Channel = DMA_CHANNEL_4;
	hdma_usart1_tx.Init.Direction = DMA_MEMORY_TO_PERIPH;
	hdma_usart1_tx.Init.PeriphInc = DMA_PINC_DISABLE;
	hdma_usart1_tx.Init.MemInc = DMA_MINC_ENABLE;
	hdma_usart1_tx.Init.PeriphDataAlignment = DMA_PDATAALIGN_BYTE;
	hdma_usart1_tx.Init.MemDataAlignment = DMA_MDATAALIGN_BYTE;
	hdma_usart1_tx.Init.Mode = DMA_NORMAL;
	hdma_usart1_tx.Init.Priority = DMA_PRIORITY_HIGH;
	hdma_usart1_tx.Init.FIFOMode = DMA_FIFOMODE_DISABLE;

	if (HAL_DMA_Init(&hdma_usart1_tx) != HAL_OK) {
		return 0U;
	}
	__HAL_LINKDMA((&huart1), hdmatx, hdma_usart1_tx);

	HAL_NVIC_SetPriority(DMA2_Stream7_IRQn, 5, 0);
	HAL_NVIC_EnableIRQ(DMA2_Stream7_IRQn);

	return 1U;
}

void CanStreamInit(void) {
	uint32_t primask = EnterCritical();

	tx_head = 0U;
	tx_tail = 0U;
	tx_chunk = 0U;
	tx_active = 0U;
	event_head = 0U;
	event_tail = 0U;
	memset(rate_limit, 0, sizeof(rate_limit));
	stream_seq = 0U;
	records_sent = 0U;
	bytes_sent = 0U;
	records_dropped = 0U;
	bytes_dropped = 0U;
	event_queue_dropped = 0U;
	dma_errors = 0U;
	frames_rejected = 0U;
	tx_peak = 0U;
	loop_max_us = 0U;

	ExitCritical(primask);

	if (CanStreamConfigureDma() == 0U) {
		Error_Handler();
	}
	if (CanStreamConfigureUart(CAN_STREAM_DEFAULT_BAUDRATE) == 0U) {
		Error_Handler();
	}

	initialised = 1U;
	time_sync_tick = HAL_GetTick();
	stats_tick = time_sync_tick;

	CanStreamEmitSession();
}

/* ------------------------------------------------------------------------- */
/* Record producers                                                          */
/* ------------------------------------------------------------------------- */

void CanStreamEmitSession(void) {
	uint8_t encoded[CAN_STREAM_MAX_ENCODED];
	CanStreamSession session;
	RTC_TimeTypeDef time = { 0 };
	RTC_DateTypeDef date = { 0 };
	size_t length;

	if (initialised == 0U) {
		return;
	}

	memset(&session, 0, sizeof(session));
	session.proto_version = (uint8_t) CAN_STREAM_PROTO_VERSION;
	session.flags = CanStreamFlags();
	session.max_record_len = (uint16_t) CAN_STREAM_MAX_RECORD;
	session.ts64 = GETTICK;
	session.can1_bitrate = CanStreamBitrate(&hcan1);
	session.can2_bitrate = CanStreamBitrate(&hcan2);
	session.can1_btr = hcan1.Instance->BTR;
	session.can2_btr = hcan2.Instance->BTR;
	session.uart_baudrate = stream_baudrate;

	if (RTCGetDateTime(&date, &time) != 0U) {
		session.rtc_valid = 1U;
		session.rtc_year = date.Year;
		session.rtc_month = date.Month;
		session.rtc_day = date.Date;
		session.rtc_hour = time.Hours;
		session.rtc_minute = time.Minutes;
		session.rtc_second = time.Seconds;
	}

	length = CanStreamEncodeSession(stream_seq, &session, encoded,
			sizeof(encoded));
	if ((length != 0U) && (TxPush(encoded, (uint16_t) length) != 0U)) {
		stream_seq++;
	}
}

static void CanStreamEmitTimeSync(void) {
	uint8_t encoded[CAN_STREAM_MAX_ENCODED];
	size_t length = CanStreamEncodeTimeSync(stream_seq, GETTICK, encoded,
			sizeof(encoded));

	if ((length != 0U) && (TxPush(encoded, (uint16_t) length) != 0U)) {
		stream_seq++;
	}
}

void CanStreamWriteFrame(const struct canframe *frame) {
	uint8_t encoded[CAN_STREAM_MAX_ENCODED];
	CanStreamFrame record;
	size_t length;

	if ((frame == NULL) || (initialised == 0U) || (stream_enabled == 0U)) {
		return;
	}
	if ((frame->id > 0x7FFU) || (frame->dlc > 8U) || (frame->can < 1U)
			|| (frame->can > 2U)) {
		frames_rejected++;
		return;
	}

	memset(&record, 0, sizeof(record));
	record.seq = stream_seq;
	record.id = (uint16_t) frame->id;
	record.dlc = frame->dlc;
	record.channel = frame->can;
	record.ts32 = (uint32_t) frame->timestamp;
	memcpy(record.data, frame->data, frame->dlc);

	length = CanStreamEncodeFrame(&record, encoded, sizeof(encoded));
	if (length == 0U) {
		frames_rejected++;
		return;
	}

	/*
	 * The sequence number is consumed only when the record really entered the
	 * transmit buffer. A gap in the sequence therefore means a loss on the
	 * serial link, while a record dropped here is reported through the
	 * records_dropped counter and a rate limited overflow event. The receiver
	 * can tell the two apart, which is what the losslessness argument needs.
	 */
	if (TxPush(encoded, (uint16_t) length) != 0U) {
		stream_seq++;
	} else {
		CanStreamPostEvent(frame->can, (uint8_t) CAN_STREAM_EVENT_TX_OVERFLOW,
				(uint16_t) frame->id, 0U, 0U, frame->timestamp);
	}
}

void CanStreamPostEvent(uint8_t bus_number, uint8_t reason, uint16_t frame_id,
		uint32_t error_code, uint8_t state, uint64_t timestamp) {
	uint32_t primask;
	uint8_t next;
	CanStreamRateLimit *limit;
	uint32_t now;

	if ((bus_number < 1U) || (bus_number > CAN_DIAG_BUS_COUNT)
			|| (reason >= (uint8_t) CAN_STREAM_EVENT_REASON_COUNT)) {
		return;
	}

	primask = EnterCritical();

	limit = &rate_limit[bus_number - 1U][reason];
	now = HAL_GetTick();
	if ((limit->seen != 0U)
			&& ((now - limit->last_tick) < CAN_STREAM_EVENT_MIN_INTERVAL_MS)) {
		limit->suppressed++;
		ExitCritical(primask);
		return;
	}
	limit->seen = 1U;
	limit->last_tick = now;

	next = (uint8_t) ((event_head + 1U) % CAN_STREAM_EVENT_QUEUE_SIZE);
	if (next == event_tail) {
		event_queue_dropped++;
		ExitCritical(primask);
		return;
	}

	event_queue[event_head].timestamp = timestamp;
	event_queue[event_head].error_code = error_code;
	event_queue[event_head].suppressed = limit->suppressed;
	event_queue[event_head].frame_id = frame_id;
	event_queue[event_head].bus_number = bus_number;
	event_queue[event_head].reason = reason;
	event_queue[event_head].state = state;
	event_head = next;
	limit->suppressed = 0U;

	ExitCritical(primask);
}

static void CanStreamDrainEvents(void) {
	while (event_tail != event_head) {
		CanStreamQueuedEvent queued;
		CanStreamEvent event;
		uint8_t encoded[CAN_STREAM_MAX_ENCODED];
		size_t length;
		uint32_t primask = EnterCritical();

		queued = event_queue[event_tail];
		ExitCritical(primask);

		memset(&event, 0, sizeof(event));
		event.ts64 = queued.timestamp;
		event.error_code = queued.error_code;
		event.suppressed = queued.suppressed;
		event.frame_id = queued.frame_id;
		event.channel = queued.bus_number;
		event.reason = queued.reason;
		event.state = queued.state;

		length = CanStreamEncodeEvent(stream_seq, &event, encoded,
				sizeof(encoded));
		if (length == 0U) {
			/* Malformed entry, drop it rather than stalling the queue. */
			event_tail = (uint8_t) ((event_tail + 1U)
					% CAN_STREAM_EVENT_QUEUE_SIZE);
			continue;
		}
		if (TxPush(encoded, (uint16_t) length) == 0U) {
			return; /* retry on the next pass, keep the entry queued */
		}
		stream_seq++;
		event_tail = (uint8_t) ((event_tail + 1U)
				% CAN_STREAM_EVENT_QUEUE_SIZE);
	}
}

static void CanStreamEmitBusStats(uint8_t bus_number) {
	CAN_DiagnosticsSnapshot_t snapshot;
	CanStreamBusStats stats;
	uint8_t encoded[CAN_STREAM_MAX_ENCODED];
	size_t length;

	/*
	 * The snapshot getter does not consume anything, so the existing USB
	 * diagnostics path that relies on CAN_DiagnosticsTakeEvent() keeps working
	 * untouched. The stream never calls the consuming getter.
	 */
	if (CAN_DiagnosticsGetSnapshot(bus_number, &snapshot) == 0U) {
		return;
	}

	memset(&stats, 0, sizeof(stats));
	stats.channel = bus_number;
	stats.ts64 = GETTICK;
	stats.rx_frames = snapshot.rx_frames;
	stats.valid_frames = snapshot.valid_frames;
	stats.buffered_frames = snapshot.buffered_frames;
	stats.payload_bytes = snapshot.payload_bytes;
	stats.rx_read_errors = snapshot.rx_read_errors;
	stats.extended_frames = snapshot.extended_frames;
	stats.remote_frames = snapshot.remote_frames;
	stats.invalid_dlc = snapshot.invalid_dlc;
	stats.fifo_full = snapshot.fifo_full;
	stats.fifo_overrun = snapshot.fifo_overrun;
	stats.ring_dropped = snapshot.ring_dropped;
	stats.warning_entries = snapshot.warning_entries;
	stats.passive_entries = snapshot.passive_entries;
	stats.bus_off_entries = snapshot.bus_off_entries;
	stats.stuff_errors = snapshot.stuff_errors;
	stats.form_errors = snapshot.form_errors;
	stats.ack_errors = snapshot.ack_errors;
	stats.bit_recessive_errors = snapshot.bit_recessive_errors;
	stats.bit_dominant_errors = snapshot.bit_dominant_errors;
	stats.crc_errors = snapshot.crc_errors;
	stats.other_errors = snapshot.other_errors;
	stats.last_error_code = snapshot.last_error_code;
	stats.last_ring_drop_id = snapshot.last_ring_drop_id;
	stats.fifo_max_fill = snapshot.fifo_max_fill;
	stats.current_rec = snapshot.current_rec;
	stats.current_tec = snapshot.current_tec;
	stats.max_rec = snapshot.max_rec;
	stats.max_tec = snapshot.max_tec;
	stats.state = CanStreamBusStateToByte(snapshot.state);

	length = CanStreamEncodeBusStats(stream_seq, &stats, encoded,
			sizeof(encoded));
	if ((length != 0U) && (TxPush(encoded, (uint16_t) length) != 0U)) {
		stream_seq++;
	}
}

static void CanStreamEmitLoggerStats(void) {
	CanStreamLoggerStats stats;
	uint8_t encoded[CAN_STREAM_MAX_ENCODED];
	size_t length;

	memset(&stats, 0, sizeof(stats));
	stats.ts64 = GETTICK;
	stats.ring_drop_total = AppGetRingDropTotal();
	stats.tx_records_sent = records_sent;
	stats.tx_bytes_sent = bytes_sent;
	stats.tx_records_dropped = records_dropped;
	stats.tx_bytes_dropped = bytes_dropped;
	stats.event_queue_dropped = event_queue_dropped;
	stats.loop_max_us = loop_max_us;
	stats.ring_peak = AppGetRingPeak();
	stats.ring_capacity = (uint16_t) (BUFFER_SIZE - 1U);
	stats.tx_buffer_peak = tx_peak;
	stats.tx_buffer_capacity = (uint16_t) (CAN_STREAM_TX_BUFFER_SIZE - 1U);
	stats.flags = CanStreamFlags();

	length = CanStreamEncodeLoggerStats(stream_seq, &stats, encoded,
			sizeof(encoded));
	if ((length != 0U) && (TxPush(encoded, (uint16_t) length) != 0U)) {
		stream_seq++;
	}
}

void CanStreamSendAck(uint8_t command, uint8_t status, uint32_t value) {
	CanStreamAck ack;
	uint8_t encoded[CAN_STREAM_MAX_ENCODED];
	size_t length;

	if (initialised == 0U) {
		return;
	}
	memset(&ack, 0, sizeof(ack));
	ack.status = status;
	ack.command = command;
	ack.value = value;
	ack.ts64 = GETTICK;

	length = CanStreamEncodeAck(stream_seq, &ack, encoded, sizeof(encoded));
	if ((length != 0U) && (TxPush(encoded, (uint16_t) length) != 0U)) {
		stream_seq++;
	}
}

/* ------------------------------------------------------------------------- */
/* Periodic work                                                             */
/* ------------------------------------------------------------------------- */

void CanStreamTask(void) {
	uint32_t now;

	if (initialised == 0U) {
		return;
	}

	CanStreamDrainEvents();

	now = HAL_GetTick();
	if ((now - time_sync_tick) >= CAN_STREAM_TIME_SYNC_PERIOD_MS) {
		time_sync_tick = now;
		if (stream_enabled != 0U) {
			CanStreamEmitTimeSync();
		}
	}
	if ((now - stats_tick) >= CAN_STREAM_STATS_PERIOD_MS) {
		stats_tick = now;
		if (stream_enabled != 0U) {
			for (uint8_t bus = 1U; bus <= CAN_DIAG_BUS_COUNT; bus++) {
				CanStreamEmitBusStats(bus);
			}
			CanStreamEmitLoggerStats();
		}
	}

	CanStreamKick();
}

/* ------------------------------------------------------------------------- */
/* Configuration                                                             */
/* ------------------------------------------------------------------------- */

void CanStreamSetEnabled(uint8_t enabled) {
	stream_enabled = (enabled != 0U) ? 1U : 0U;
	if (stream_enabled != 0U) {
		/* A receiver attaching later needs the session and the time base. */
		CanStreamEmitSession();
		CanStreamEmitTimeSync();
	}
}

uint8_t CanStreamIsEnabled(void) {
	return stream_enabled;
}

void CanStreamSetMeasurementMode(uint8_t enabled) {
	measurement_mode = (enabled != 0U) ? 1U : 0U;
}

void CanStreamSetUsbLogging(uint8_t active) {
	usb_logging = (active != 0U) ? 1U : 0U;
}

uint32_t CanStreamGetBaudrate(void) {
	return stream_baudrate;
}

uint8_t CanStreamSetBaudrate(uint32_t baudrate) {
	uint32_t deadline;

	if ((baudrate < CAN_STREAM_MIN_BAUDRATE)
			|| (baudrate > CAN_STREAM_MAX_BAUDRATE)) {
		return 0U;
	}
	if (baudrate == stream_baudrate) {
		return 1U;
	}

	/*
	 * The acknowledgement still leaves at the old speed, so the buffer is
	 * drained first. The wait is bounded and refreshes the watchdog, because a
	 * stalled link must not turn a configuration change into a reset.
	 */
	deadline = HAL_GetTick() + 200U;
	while ((TxUsed() != 0U) && ((int32_t) (HAL_GetTick() - deadline) < 0)) {
		WatchdogRefresh();
		CanStreamKick();
	}

	(void) HAL_UART_DMAStop(&huart1);
	tx_active = 0U;
	tx_chunk = 0U;
	tx_head = 0U;
	tx_tail = 0U;

	if (CanStreamConfigureUart(baudrate) == 0U) {
		return 0U;
	}

	CanStreamEmitSession();
	CanStreamEmitTimeSync();
	return 1U;
}

void CanStreamNoteLoopTime(uint32_t microseconds) {
	if (microseconds > loop_max_us) {
		loop_max_us = microseconds;
	}
}

int CanStreamFormatSummary(char *out, size_t size) {
	if ((out == NULL) || (size == 0U)) {
		return 0;
	}
	return snprintf(out, size,
			"stream=%u baud=%lu seq=%u sent=%lu bytes=%lu drop_rec=%lu "
			"drop_bytes=%lu txpeak=%u evtdrop=%lu dmaerr=%lu rej=%lu "
			"loopmax=%luus\r\n", (unsigned int) stream_enabled,
			(unsigned long) stream_baudrate, (unsigned int) stream_seq,
			(unsigned long) records_sent, (unsigned long) bytes_sent,
			(unsigned long) records_dropped, (unsigned long) bytes_dropped,
			(unsigned int) tx_peak, (unsigned long) event_queue_dropped,
			(unsigned long) dma_errors, (unsigned long) frames_rejected,
			(unsigned long) loop_max_us);
}

/* ------------------------------------------------------------------------- */
/* Interrupt plumbing                                                        */
/* ------------------------------------------------------------------------- */

void DMA2_Stream7_IRQHandler(void) {
	HAL_DMA_IRQHandler(&hdma_usart1_tx);
}

void HAL_UART_TxCpltCallback(UART_HandleTypeDef *huart) {
	if (huart->Instance != USART1) {
		return; /* the debug channel still uses interrupt driven writes */
	}

	tx_tail = (uint16_t) ((tx_tail + tx_chunk) % CAN_STREAM_TX_BUFFER_SIZE);
	bytes_sent += tx_chunk;
	tx_chunk = 0U;
	tx_active = 0U;

	CanStreamKick();
}

void HAL_UART_ErrorCallback(UART_HandleTypeDef *huart) {
	if (huart->Instance != USART1) {
		return;
	}
	dma_errors++;
	tx_active = 0U;
	tx_chunk = 0U;
	CanStreamKick();
}
