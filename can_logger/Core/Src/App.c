/*
 * App.c
 *
 *  Created on: May 10, 2023
 *      Author: kubak
 */
#include "main.h"
#include "App.h"
#include <stdio.h>
#include "usb_host.h"
#include "fatfs.h"
#include "parsing.h"
#include "caninit.h"
#include "can_diagnostics.h"
#include "can_stream.h"
#include "rtc.h"
#include "ring_buffer.h"
#include "usb_interface.h"
#include "watchdog.h"

#include "UART_DMA.h"
#include "string.h"

extern UART_HandleTypeDef huart2;
extern UART_HandleTypeDef huart1;

UARTDMA_HandleTypeDef huartdma;
char ParseBuffer[8];

//	External variables related to HAL

extern ApplicationTypeDef Appli_state;

uint8_t descriptor_ready = 0;

/*
 * Accepts frames with a 29-bit identifier whose value fits in 11 bits, carrying
 * them as standard frames. Needed because the bench transmitter sends extended
 * frames only. Set to 0 to enforce the 11-bit rule strictly, which is what a
 * measurement of a real CAN 2.0A bus should use.
 */
#define CAN_ACCEPT_EXTENDED_AS_STANDARD 1

//	Auxiliary logging variables
uint16_t textlen_uartdebug;
/* Large enough for the stream summary with every counter at its maximum. */
char text_uartdebug[256];

//	Pendrive State
enum penstate {
	DISCONNECTED = 1, INIT, CONNECTED, DEINIT
} penstate;

static RingBuffer_t buf;

/*
 * Measurement mode suspends the USB log so that the throughput of the data
 * channel does not depend on file system operations. The pen state machine owns
 * the flag, can_stream only mirrors it into the reported session state.
 */
static uint8_t measurement_mode;

//	Init functions
void AppInit(void);
uint8_t PenInit(void);
void PenDeinit(void);

//	Other functions
void ParseReceivedFrames(void);
void UartReceive(void);
uint8_t IsPenConnected(void);

//	General Task Functions
void PenTask();
void LedTask(void);
void UartDebugTask(void);
void UartReceiveTask(void);
void ParseReceivedFramesTask(void);
void NewDescriptorTask(void);
void USBWriteStrBufferTask(void);
static void CAN_DiagnosticsTask(void);
static uint8_t CAN_LogSummary(void);
static uint8_t CAN_LogSession(void);
static void MeasurementModeSet(uint8_t enabled);

static void DumpCanRegisters(void);
static void SelfTestOnBootTask(void);

static void AppEnterCpuSleep(void) {
	CLEAR_BIT(SCB->SCR, SCB_SCR_SLEEPDEEP_Msk);
	__DSB();
	__WFI();
	__ISB();
}

void AppRun(void) {
	while (1) {
		/*
		 * The iteration is timed because the watchdog window bounds how long a
		 * single pass may take. The measured maximum is reported in the logger
		 * statistics record, so the margin is an observed value rather than an
		 * assumption.
		 */
		uint64_t iteration_start = GETTICK;

		WatchdogRefresh();
		MX_USB_HOST_Process();
		PenTask();
		LedTask();
		UartDebugTask();
		ParseReceivedFrames();
		CanStreamTask();
		CAN_DiagnosticsTask();
		UartReceiveTask();
		USBWriteStrBufferTask();
		NewDescriptorTask();

		SelfTestOnBootTask();

		CanStreamNoteLoopTime((uint32_t) (GETTICK - iteration_start));
		AppEnterCpuSleep();
	}
}

uint16_t AppGetRingPeak(void) {
	return buf.lenmax;
}

uint32_t AppGetRingDropTotal(void) {
	return buf.missed_data;
}

uint8_t AppIsMeasurementMode(void) {
	return measurement_mode;
}

static char filename[22] = { 0 };

static uint8_t NewDescriptor(void) {
	RTC_TimeTypeDef time = { 0 };
	RTC_DateTypeDef date = { 0 };
	int length;

	if (RTCGetDateTime(&date, &time) == 0U) {
		return 0U;
	}
	length = snprintf(filename, sizeof(filename),
			"20%02u%02u%02u%02u%02u.log", date.Year, date.Month,
			date.Date, time.Hours, time.Minutes);
	if ((length < 0) || ((size_t) length >= sizeof(filename))) {
		return 0U;
	}
	if ((descriptor_ready != 0U) && (CAN_LogSummary() == 0U)) {
		return 0U;
	}

	printk("new_file_start");
	printk("filename");
	printk_lld("HAL_GetTick:", HAL_GetTick());

	descriptor_ready = USBNewDescriptor(filename);
	if ((descriptor_ready != 0U) && (CAN_LogSession() == 0U)) {
		descriptor_ready = 0U;
	}
	return descriptor_ready;
}

void NewDescriptorTask(void) {
	static uint32_t tick = 0;
	if (penstate == CONNECTED && Appli_state == APPLICATION_READY) {
		if (HAL_GetTick() - tick > 1000 * 60) {
			tick = HAL_GetTick();
			if (NewDescriptor() == 0U) {
				USB_Error_Handler();
			}
		}
	}
}

void USBWriteStrBufferTask(void) {
	if (USBCheckBufferTickSend()) {
		if (IsPenConnected()) {
			if (USBSyncStrBuffer() == 0U) {
				USB_Error_Handler();
			}
		}
	}
}

void LedTask(void) {
	static uint32_t ledtick;
	if (HAL_GetTick() - ledtick > 600) {
		ledtick = HAL_GetTick();
		HAL_GPIO_WritePin(LED1_GPIO_Port, LED1_Pin, 1);
	}
}

/*
 * Diagnostics for the operator go to the debug channel only. The data channel
 * carries binary records exclusively, so a text message can never be mistaken
 * for a record and cannot steal bandwidth from the stream.
 */
void UartDebugTask(void) {
	static uint32_t uarttick;

	if (HAL_GetTick() - uarttick > 5000) {
		uarttick = HAL_GetTick();

		int written = CanStreamFormatSummary(text_uartdebug,
				sizeof(text_uartdebug));

		/* snprintf reports the untruncated length, so it has to be clamped. */
		if (written < 0) {
			written = 0;
		} else if ((size_t) written >= sizeof(text_uartdebug)) {
			written = (int) (sizeof(text_uartdebug) - 1U);
		}
		textlen_uartdebug = (uint16_t) written;

		if (textlen_uartdebug > 0U) {
			(void) HAL_UART_Transmit_IT(&huart_debug,
					(uint8_t*) text_uartdebug, textlen_uartdebug);
		}
	}
}

void UartReceiveTask(void) {
	static uint32_t uartreceivetick = 0;
	if (HAL_GetTick() - uartreceivetick > 100) {
		uartreceivetick = HAL_GetTick();
		UartReceive();
	}
}

uint8_t PenInit(void) {
	static int pinit = 0;

	textlen_uartdebug = sprintf(text_uartdebug, "PenInit: %d\n\r", pinit++);
	HAL_UART_Transmit_IT(&huart_debug, (uint8_t*) text_uartdebug,
			textlen_uartdebug);

	if (USBMount() == 0U) {
		return 0U;
	}
	if (NewDescriptor() == 0U) {
		/*
		 * No USBCloseDescriptor here on purpose. There is no open file to close
		 * at this point, and that call zeroes the text buffer, which discarded
		 * everything logged before the drive was mounted. Since a failed mount
		 * is retried once a second, early boot diagnostics never survived to
		 * reach a file.
		 */
		USBUnmount();
		return 0U;
	}
	printk("log_start");

	return 1U;
}

uint8_t IsPenConnected(void) {
	return (penstate == CONNECTED && Appli_state == APPLICATION_READY
			&& descriptor_ready);
}

/*
 * Single consumer of the ring buffer. Both sinks are served from the same read,
 * so the file on the USB drive and the stream towards the Raspberry Pi observe
 * exactly the same frames in exactly the same order. That is what makes the two
 * comparable when the losslessness of the link is evaluated.
 */
void ParseReceivedFrames(void) {

	struct canframe frame_ = { 0 };

	while (ReadFromBuffer(&buf, &frame_) == 0) {
		if (IsPenConnected()) {
			if (IsUSBBufferFull()) {
				if (USBSyncStrBuffer() == 0U) {
					USB_Error_Handler();
				}
			}
			if (IsPenConnected()) {
				char text_frame[37] = { 0 };
				uint8_t text_length = ParsePenFrame(&frame_, text_frame);
				if ((text_length == 0U)
						|| (USBWriteStr2Buffer(text_frame, text_length) == 0U)) {
					USB_Error_Handler();
				}
			}
		}
		CanStreamWriteFrame(&frame_);
	}
}

static const char *CAN_DiagnosticsStateName(CAN_DiagnosticsState_t state) {
	switch (state) {
	case CAN_DIAG_STATE_WARNING:
		return "warning";
	case CAN_DIAG_STATE_PASSIVE:
		return "passive";
	case CAN_DIAG_STATE_BUS_OFF:
		return "bus_off";
	case CAN_DIAG_STATE_ACTIVE:
	default:
		return "active";
	}
}

static uint32_t CAN_GetConfiguredBitrate(const CAN_HandleTypeDef *hcan) {
	uint32_t time_quanta;
	uint32_t segment1;
	uint32_t segment2;

	if ((hcan == NULL) || (hcan->Init.Prescaler == 0U)) {
		return 0U;
	}
	segment1 = ((hcan->Init.TimeSeg1 & CAN_BTR_TS1) >> CAN_BTR_TS1_Pos) + 1U;
	segment2 = ((hcan->Init.TimeSeg2 & CAN_BTR_TS2) >> CAN_BTR_TS2_Pos) + 1U;
	time_quanta = 1U + segment1 + segment2;
	return HAL_RCC_GetPCLK1Freq() / hcan->Init.Prescaler / time_quanta;
}

static uint8_t CAN_WriteDiagnosticLine(const char *line, int length,
		size_t capacity) {
	if ((line == NULL) || (length <= 0) || ((size_t) length >= capacity)) {
		return 0U;
	}
	return USBWriteStr2Buffer(line, (size_t) length);
}

static uint8_t CAN_LogSession(void) {
	char line[224];
	int length = snprintf(line, sizeof(line),
			"#SESSION v=1 frame_format=2 t=%llu can1_bitrate=%lu can2_bitrate=%lu "
			"can1_btr=0x%08lX can2_btr=0x%08lX mode1=0x%08lX "
			"mode2=0x%08lX abom1=%u abom2=%u\r\n",
			(unsigned long long) GETTIME,
			(unsigned long) CAN_GetConfiguredBitrate(&hcan1),
			(unsigned long) CAN_GetConfiguredBitrate(&hcan2),
			(unsigned long) hcan1.Instance->BTR,
			(unsigned long) hcan2.Instance->BTR,
			(unsigned long) hcan1.Init.Mode,
			(unsigned long) hcan2.Init.Mode,
			(unsigned int) hcan1.Init.AutoBusOff,
			(unsigned int) hcan2.Init.AutoBusOff);

	return CAN_WriteDiagnosticLine(line, length, sizeof(line));
}

static uint8_t CAN_LogBusSummary(uint8_t bus_number, uint64_t timestamp) {
	CAN_DiagnosticsSnapshot_t snapshot;
	char line[384];
	int length;

	if (CAN_DiagnosticsGetSnapshot(bus_number, &snapshot) == 0U) {
		return 0U;
	}
	length = snprintf(line, sizeof(line),
			"#CANSTAT v=1 scope=boot t=%llu bus=%u rx=%lu valid=%lu "
			"queued=%lu payload_bytes=%lu read_err=%lu ext=%lu rtr=%lu "
			"dlc_err=%lu fifo_full=%lu fifo_ovr=%lu ring_drop=%lu "
			"last_drop_id=0x%03lX fifo_peak=%u state=%s rec=%u tec=%u "
			"rec_peak=%u tec_peak=%u\r\n",
			(unsigned long long) timestamp, (unsigned int) bus_number,
			(unsigned long) snapshot.rx_frames,
			(unsigned long) snapshot.valid_frames,
			(unsigned long) snapshot.buffered_frames,
			(unsigned long) snapshot.payload_bytes,
			(unsigned long) snapshot.rx_read_errors,
			(unsigned long) snapshot.extended_frames,
			(unsigned long) snapshot.remote_frames,
			(unsigned long) snapshot.invalid_dlc,
			(unsigned long) snapshot.fifo_full,
			(unsigned long) snapshot.fifo_overrun,
			(unsigned long) snapshot.ring_dropped,
			(unsigned long) snapshot.last_ring_drop_id,
			(unsigned int) snapshot.fifo_max_fill,
			CAN_DiagnosticsStateName(snapshot.state),
			(unsigned int) snapshot.current_rec,
			(unsigned int) snapshot.current_tec,
			(unsigned int) snapshot.max_rec,
			(unsigned int) snapshot.max_tec);
	if (CAN_WriteDiagnosticLine(line, length, sizeof(line)) == 0U) {
		return 0U;
	}

	length = snprintf(line, sizeof(line),
			"#CANERR v=1 scope=boot t=%llu bus=%u warning=%lu "
			"passive=%lu bus_off=%lu stuff=%lu form=%lu ack=%lu bit_r=%lu "
			"bit_d=%lu crc=%lu other=%lu last=0x%08lX\r\n",
			(unsigned long long) timestamp, (unsigned int) bus_number,
			(unsigned long) snapshot.warning_entries,
			(unsigned long) snapshot.passive_entries,
			(unsigned long) snapshot.bus_off_entries,
			(unsigned long) snapshot.stuff_errors,
			(unsigned long) snapshot.form_errors,
			(unsigned long) snapshot.ack_errors,
			(unsigned long) snapshot.bit_recessive_errors,
			(unsigned long) snapshot.bit_dominant_errors,
			(unsigned long) snapshot.crc_errors,
			(unsigned long) snapshot.other_errors,
			(unsigned long) snapshot.last_error_code);
	return CAN_WriteDiagnosticLine(line, length, sizeof(line));
}

static uint8_t CAN_LogSummary(void) {
	char line[144];
	uint64_t timestamp = GETTIME;
	int length;

	CAN_DiagnosticsObserveController(1U, hcan1.Instance->ESR, GETTICK);
	CAN_DiagnosticsObserveController(2U, hcan2.Instance->ESR, GETTICK);
	if ((CAN_LogBusSummary(1U, timestamp) == 0U)
			|| (CAN_LogBusSummary(2U, timestamp) == 0U)) {
		return 0U;
	}

	length = snprintf(line, sizeof(line),
			"#LOGGERSTAT v=1 t=%llu ring_peak=%u ring_capacity=%u "
			"ring_drop_total=%lu\r\n",
			(unsigned long long) timestamp, (unsigned int) buf.lenmax,
			(unsigned int) (BUFFER_SIZE - 1U),
			(unsigned long) buf.missed_data);
	return CAN_WriteDiagnosticLine(line, length, sizeof(line));
}

static void CAN_DiagnosticsTask(void) {
	static uint32_t event_tick;
	CAN_HandleTypeDef *handles[CAN_DIAG_BUS_COUNT] = { &hcan1, &hcan2 };
	uint64_t timestamp = GETTICK;
	uint32_t now = HAL_GetTick();

	for (uint8_t index = 0U; index < CAN_DIAG_BUS_COUNT; index++) {
		CAN_DiagnosticsObserveController((uint8_t) (index + 1U),
				handles[index]->Instance->ESR, timestamp);
	}
	if ((IsPenConnected() == 0U) || ((now - event_tick) < 1000U)) {
		return;
	}
	event_tick = now;

	for (uint8_t index = 0U; index < CAN_DIAG_BUS_COUNT; index++) {
		CAN_DiagnosticsEvent_t event;
		CAN_DiagnosticsSnapshot_t snapshot;
		char line[224];
		int length;
		uint8_t bus_number = (uint8_t) (index + 1U);

		if (CAN_DiagnosticsTakeEvent(bus_number, &event) == 0U) {
			continue;
		}
		if (CAN_DiagnosticsGetSnapshot(bus_number, &snapshot) == 0U) {
			continue;
		}
		length = snprintf(line, sizeof(line),
				"#CANEVENT v=1 t=%llu bus=%u flags=0x%08lX state=%s "
				"rec=%u tec=%u last=0x%08lX last_drop_id=0x%03lX\r\n",
				(unsigned long long) gettime(event.timestamp),
				(unsigned int) bus_number, (unsigned long) event.flags,
				CAN_DiagnosticsStateName(snapshot.state),
				(unsigned int) snapshot.current_rec,
				(unsigned int) snapshot.current_tec,
				(unsigned long) event.last_error_code,
				(unsigned long) snapshot.last_ring_drop_id);
		if (CAN_WriteDiagnosticLine(line, length, sizeof(line)) == 0U) {
			USB_Error_Handler();
			return;
		}
	}
}

const char str[] = "\r\ncommands:\r\n"
		"  set date %02d %02d %04d\r\n"
		"  set time %02d %02d\r\n"
		"  stream on | stream off\r\n"
		"  stream baud <9600..4000000>\r\n"
		"  stream stats\r\n"
		"  measure on | measure off\r\n"
		"  can selftest [1|2]\r\n"
		"  can regs\r\n";

/* Nine frames sweep DLC 0 to 8, which also exercises the record length. */
#define CAN_SELFTEST_BASE_ID 0x600U
#define CAN_SELFTEST_FRAMES 9U

/*
 * Runs the loopback self test once a few seconds after start-up and writes the
 * controller registers before and after it. Off by default, because it injects
 * nine synthetic frames with identifiers 0x600 to 0x608 into both the file and
 * the stream, which has no place in a measurement session. Set to 1 to bring the
 * check back when the receive path is in doubt; the same test is available at any
 * time through the `can selftest` command on the debug channel.
 */
#define CAN_SELFTEST_ON_BOOT 0

static uint8_t ParseDecimal(const char *text, size_t length, uint16_t *value) {
	uint16_t parsed = 0U;

	if ((text == NULL) || (value == NULL) || (length == 0U)) {
		return 0U;
	}
	for (size_t i = 0U; i < length; i++) {
		if ((text[i] < '0') || (text[i] > '9')) {
			return 0U;
		}
		parsed = (uint16_t) (parsed * 10U + (uint16_t) (text[i] - '0'));
	}
	*value = parsed;
	return 1U;
}

static uint8_t ParseU32(const char *text, size_t length, uint32_t *value) {
	uint32_t parsed = 0U;

	if ((text == NULL) || (value == NULL) || (length == 0U)
			|| (length > 10U)) {
		return 0U;
	}
	for (size_t i = 0U; i < length; i++) {
		if ((text[i] < '0') || (text[i] > '9')) {
			return 0U;
		}
		if (parsed > ((0xFFFFFFFFU - (uint32_t) (text[i] - '0')) / 10U)) {
			return 0U; /* would overflow */
		}
		parsed = (parsed * 10U) + (uint32_t) (text[i] - '0');
	}
	*value = parsed;
	return 1U;
}

static void UartSendText(const char *text) {
	if (text != NULL) {
		(void) HAL_UART_Transmit(&huart_debug, (uint8_t*) text,
				(uint16_t) strlen(text), 100U);
	}
}

/*
 * Raw state of one CAN controller. This answers, without a debugger, the three
 * questions that a silent receiver raises: is the controller out of
 * initialisation mode and listening, are the receive interrupts actually
 * enabled in the peripheral and in the NVIC, and is the receive queue filling
 * up. A non-zero fmp together with a receive counter stuck at zero would mean
 * frames arrive but the interrupt never runs. Both at zero means nothing is
 * reaching the controller at all.
 */
static void DumpCanBusRegisters(const CAN_HandleTypeDef *hcan, uint8_t bus,
		IRQn_Type rx_irq, IRQn_Type sce_irq, uint32_t fmp) {
	static char message[288];
	const CAN_TypeDef *can = hcan->Instance;
	int written;

	/*
	 * One line per bus, so the same text is readable on the debug channel and in
	 * the file on the USB drive. The file path matters because it needs no extra
	 * hardware on the debug pins.
	 */
	written = snprintf(message, sizeof(message),
			"#CANREGS bus=%u state=%u mcr=%08lX msr=%08lX tsr=%08lX "
			"rf0r=%08lX rf1r=%08lX ier=%08lX esr=%08lX btr=%08lX "
			"fmp=%lu inak=%u slak=%u listening=%u nvic_rx=%u nvic_sce=%u "
			"lec=%lu rec=%lu tec=%lu boff=%u epv=%u ewg=%u",
			(unsigned int) bus, (unsigned int) hcan->State,
			(unsigned long) can->MCR, (unsigned long) can->MSR,
			(unsigned long) can->TSR, (unsigned long) can->RF0R,
			(unsigned long) can->RF1R, (unsigned long) can->IER,
			(unsigned long) can->ESR, (unsigned long) can->BTR,
			(unsigned long) fmp,
			(unsigned int) ((can->MSR & CAN_MSR_INAK) != 0U),
			(unsigned int) ((can->MSR & CAN_MSR_SLAK) != 0U),
			(unsigned int) (hcan->State == HAL_CAN_STATE_LISTENING),
			(unsigned int) (NVIC_GetEnableIRQ(rx_irq) != 0U),
			(unsigned int) (NVIC_GetEnableIRQ(sce_irq) != 0U),
			(unsigned long) ((can->ESR & CAN_ESR_LEC) >> CAN_ESR_LEC_Pos),
			(unsigned long) ((can->ESR & CAN_ESR_REC) >> CAN_ESR_REC_Pos),
			(unsigned long) ((can->ESR & CAN_ESR_TEC) >> CAN_ESR_TEC_Pos),
			(unsigned int) ((can->ESR & CAN_ESR_BOFF) != 0U),
			(unsigned int) ((can->ESR & CAN_ESR_EPVF) != 0U),
			(unsigned int) ((can->ESR & CAN_ESR_EWGF) != 0U));

	if (written > 0) {
		printk(message); /* into the file on the USB drive */
		UartSendText(message);
		UartSendText("\r\n");
	}
}

static void DumpCanRegisters(void) {
	DumpCanBusRegisters(&hcan1, 1U, CAN1_RX0_IRQn, CAN1_SCE_IRQn,
			hcan1.Instance->RF0R & CAN_RF0R_FMP0);
	DumpCanBusRegisters(&hcan2, 2U, CAN2_RX1_IRQn, CAN2_SCE_IRQn,
			hcan2.Instance->RF1R & CAN_RF1R_FMP1);
}

static void SelfTestOnBootTask(void) {
#if CAN_SELFTEST_ON_BOOT
	static uint8_t done;
	uint8_t queued;

	/*
	 * Runs at a fixed moment after start-up, independent of the USB drive. The
	 * previous version waited for the drive so the text would land in a file,
	 * which made the test unobservable when the drive appeared minutes later.
	 * The outcome now also travels on the data channel as an acknowledgement
	 * record, so the receiver can tell whether the test ran even with no drive
	 * attached at all.
	 */
	if ((done != 0U) || (HAL_GetTick() < 3000U)) {
		return;
	}
	done = 1U;

	printk("selftest_begin");
	DumpCanRegisters();

	queued = CANSelfTest(1U, CAN_SELFTEST_BASE_ID, CAN_SELFTEST_FRAMES);

	printk_lld("selftest_queued", (uint64_t) queued);
	CanStreamSendAck((uint8_t) CAN_STREAM_CMD_SELFTEST,
			(queued == CAN_SELFTEST_FRAMES) ? 0U : 1U, queued);

	DumpCanRegisters();
	printk("selftest_end");

	/*
	 * The controller error status register of bus 1 goes out on the data channel
	 * too, so the receiver sees whether the controller is listening without
	 * needing a terminal on the debug pins.
	 */
	CanStreamSendAck((uint8_t) CAN_STREAM_CMD_SELFTEST, 0U,
			hcan1.Instance->ESR);
#endif
}

/* Returns 1 when the line was a stream command, handled or rejected. */
static uint8_t HandleStreamCommand(const char *line, size_t length) {
	if ((length == 9U) && (memcmp(line, "stream on", 9U) == 0)) {
		CanStreamSetEnabled(1U);
		CanStreamSendAck((uint8_t) CAN_STREAM_CMD_STREAM_ENABLE, 0U, 1U);
		UartSendText("stream enabled\r\n");
		return 1U;
	}
	if ((length == 10U) && (memcmp(line, "stream off", 10U) == 0)) {
		CanStreamSetEnabled(0U);
		CanStreamSendAck((uint8_t) CAN_STREAM_CMD_STREAM_ENABLE, 0U, 0U);
		UartSendText("stream disabled\r\n");
		return 1U;
	}
	if ((length == 12U) && (memcmp(line, "stream stats", 12U) == 0)) {
		static char summary[256];

		if (CanStreamFormatSummary(summary, sizeof(summary)) > 0) {
			UartSendText(summary);
		}
		return 1U;
	}
	if ((length > 12U) && (memcmp(line, "stream baud ", 12U) == 0)) {
		uint32_t baudrate = 0U;

		if ((ParseU32(&line[12], length - 12U, &baudrate) == 0U)
				|| (CanStreamSetBaudrate(baudrate) == 0U)) {
			CanStreamSendAck((uint8_t) CAN_STREAM_CMD_BAUDRATE, 1U, baudrate);
			UartSendText("baud rejected\r\n");
			return 1U;
		}
		CanStreamSendAck((uint8_t) CAN_STREAM_CMD_BAUDRATE, 0U, baudrate);
		UartSendText("baud accepted\r\n");
		return 1U;
	}
	if ((length == 8U) && (memcmp(line, "can regs", 8U) == 0)) {
		DumpCanRegisters();
		return 1U;
	}
	if ((length >= 12U) && (memcmp(line, "can selftest", 12U) == 0)) {
		static char message[160];
		uint32_t bus = 1U;
		uint8_t queued;
		int written;

		if ((length > 13U) && (line[12] == ' ')) {
			if ((ParseU32(&line[13], length - 13U, &bus) == 0U)
					|| (bus < 1U) || (bus > 2U)) {
				UartSendText("usage: can selftest [1|2]\r\n");
				return 1U;
			}
		}

		/*
		 * Silent loopback: the controller transmits to itself without driving
		 * the bus. Frames that come back travel the ordinary path, so seeing
		 * them on the Raspberry Pi proves the receive chain end to end and
		 * separates a software fault from a wiring or bit rate problem.
		 */
		queued = CANSelfTest((uint8_t) bus, CAN_SELFTEST_BASE_ID,
				CAN_SELFTEST_FRAMES);

		written = snprintf(message, sizeof(message),
				"selftest bus=%lu queued=%u base_id=0x%03X frames=%u\r\n"
				"expect %u frames with id 0x%03X..0x%03X and dlc 0..8\r\n",
				(unsigned long) bus, (unsigned int) queued,
				(unsigned int) CAN_SELFTEST_BASE_ID,
				(unsigned int) CAN_SELFTEST_FRAMES,
				(unsigned int) queued, (unsigned int) CAN_SELFTEST_BASE_ID,
				(unsigned int) (CAN_SELFTEST_BASE_ID + CAN_SELFTEST_FRAMES
						- 1U));
		if (written > 0) {
			UartSendText(message);
		}
		CanStreamSendAck((uint8_t) CAN_STREAM_CMD_SELFTEST,
				(queued == CAN_SELFTEST_FRAMES) ? 0U : 1U, queued);
		return 1U;
	}
	if ((length == 10U) && (memcmp(line, "measure on", 10U) == 0)) {
		MeasurementModeSet(1U);
		UartSendText("measurement mode on, usb log suspended\r\n");
		return 1U;
	}
	if ((length == 11U) && (memcmp(line, "measure off", 11U) == 0)) {
		MeasurementModeSet(0U);
		UartSendText("measurement mode off, usb log resumes\r\n");
		return 1U;
	}
	return 0U;
}

static uint8_t IsLeapYear(uint16_t year) {
	return (((year % 4U) == 0U) && (((year % 100U) != 0U)
			|| ((year % 400U) == 0U))) ? 1U : 0U;
}

static uint8_t IsValidDate(uint16_t year, uint16_t month, uint16_t day) {
	static const uint8_t days_per_month[] = {
			31U, 28U, 31U, 30U, 31U, 30U,
			31U, 31U, 30U, 31U, 30U, 31U
	};
	uint8_t max_day;

	if ((year < 2000U) || (year > 2099U)
			|| (month < 1U) || (month > 12U)) {
		return 0U;
	}
	max_day = days_per_month[month - 1U];
	if ((month == 2U) && (IsLeapYear(year) != 0U)) {
		max_day = 29U;
	}
	return ((day >= 1U) && (day <= max_day)) ? 1U : 0U;
}

static void UartPrintHelp(void) {
	static char response[sizeof(str) + 16U];
	RTC_TimeTypeDef time = { 0 };
	RTC_DateTypeDef date = { 0 };
	int length;

	if (RTCGetDateTime(&date, &time) == 0U) {
		return;
	}
	length = snprintf(response, sizeof(response), str,
			date.Date, date.Month, date.Year + 2000U,
			time.Hours, time.Minutes);

	if (length > 0) {
		uint16_t tx_length = (uint16_t) length;
		if ((size_t) length >= sizeof(response)) {
			tx_length = (uint16_t) (sizeof(response) - 1U);
		}
		(void) HAL_UART_Transmit(&huart_debug, (uint8_t*) response,
				tx_length, 100U);
	}
}

void UartReceive(void) {
	static const uint8_t received_header[] = "Odebrano:\r\n";
	const char *line;
	size_t length;
	uint16_t first;
	uint16_t second;
	uint16_t year;

	if (UARTDMA_GetLineFromBuffer(&huartdma) != 0) {
		return;
	}

	line = huartdma.Line_Buffer;
	length = strlen(line);
	(void) HAL_UART_Transmit(&huart_debug, (uint8_t*) received_header,
			(uint16_t) (sizeof(received_header) - 1U), 100U);
	(void) HAL_UART_Transmit(&huart_debug, (uint8_t*) line,
			(uint16_t) length, 100U);

	if (HandleStreamCommand(line, length) != 0U) {
		return;
	}

	if ((length == 19U) && (memcmp(line, "set date ", 9U) == 0)
			&& (line[11] == ' ') && (line[14] == ' ')
			&& (ParseDecimal(&line[9], 2U, &first) != 0U)
			&& (ParseDecimal(&line[12], 2U, &second) != 0U)
			&& (ParseDecimal(&line[15], 4U, &year) != 0U)
			&& (IsValidDate(year, second, first) != 0U)) {
		/*
		 * Setting the clock reboots the device, so the receiver is told that the
		 * current session ends here and a new session record will follow.
		 */
		CanStreamSendAck((uint8_t) CAN_STREAM_CMD_RTC_SET, 0U,
				(uint32_t) year);
		RTCSetDate(year, (uint8_t) second, (uint8_t) first);
		return;
	}

	if ((length == 14U) && (memcmp(line, "set time ", 9U) == 0)
			&& (line[11] == ' ')
			&& (ParseDecimal(&line[9], 2U, &first) != 0U)
			&& (ParseDecimal(&line[12], 2U, &second) != 0U)
			&& (first < 24U) && (second < 60U)) {
		CanStreamSendAck((uint8_t) CAN_STREAM_CMD_RTC_SET, 0U,
				(uint32_t) ((first * 100U) + second));
		RTCSetTime((uint8_t) first, (uint8_t) second);
		return;
	}

	UartPrintHelp();
}

void PenDeinit(void) {
	textlen_uartdebug = sprintf(text_uartdebug, "PenDeinit\r\n");
	HAL_UART_Transmit_IT(&huart_debug, (uint8_t*) text_uartdebug,
			textlen_uartdebug);
	USBCloseDescriptor();
	USBUnmount();
	descriptor_ready = 0;
	CanStreamSetUsbLogging(0U);
	CanStreamPostEvent(1U, (uint8_t) CAN_STREAM_EVENT_USB_LOG_BOUNDARY,
			CAN_STREAM_FRAME_ID_UNKNOWN, 0U, 0U, GETTICK);
}

static void MeasurementModeSet(uint8_t enabled) {
	measurement_mode = (enabled != 0U) ? 1U : 0U;
	CanStreamSetMeasurementMode(measurement_mode);

	if ((measurement_mode != 0U) && (penstate == CONNECTED)) {
		penstate = DEINIT; /* flushes and closes the file on the next pass */
	}
	CanStreamPostEvent(1U, (uint8_t) CAN_STREAM_EVENT_USB_LOG_BOUNDARY,
			CAN_STREAM_FRAME_ID_UNKNOWN, measurement_mode, 0U, GETTICK);
	CanStreamSendAck((uint8_t) CAN_STREAM_CMD_MEASUREMENT_MODE, 0U,
			measurement_mode);
}

void PenTask() {
	static uint32_t retry_tick;

	CanStreamSetUsbLogging(IsPenConnected());

	switch (penstate) {
	case DISCONNECTED:
		/*
		 * The ring buffer is no longer cleared here. It used to be wiped on
		 * every pass while no drive was present, which made streaming without a
		 * USB drive impossible. ParseReceivedFrames drains the buffer
		 * unconditionally, so nothing accumulates.
		 */
		if ((measurement_mode == 0U) && (Appli_state == APPLICATION_READY)
				&& ((HAL_GetTick() - retry_tick) >= 1000U)) {
			penstate = INIT;
		}
		break;

	case INIT:
		if ((Appli_state == APPLICATION_READY) && (measurement_mode == 0U)) {
			if (PenInit() != 0U) {
				penstate = CONNECTED;
			} else {
				retry_tick = HAL_GetTick();
				penstate = DISCONNECTED;
			}
		} else {
			penstate = DISCONNECTED;
		}
		break;

	case CONNECTED:
		if ((Appli_state != APPLICATION_READY) || (measurement_mode != 0U)) {
			penstate = DEINIT;
		}
		break;

	case DEINIT:
		PenDeinit();
		retry_tick = HAL_GetTick();
		penstate = DISCONNECTED;
		break;

	default:
		Error_Handler();
		break;
	}
}

static void CAN_ReceiveFrame(CAN_HandleTypeDef *hcan, uint32_t fifo,
		uint8_t bus_number) {
	struct canframe frame = { 0 };
	CAN_RxHeaderTypeDef header = { 0 };

	uint64_t timestamp = GETTICK;

	if (HAL_CAN_GetRxMessage(hcan, fifo, &header, frame.data) != HAL_OK) {
		CAN_DiagnosticsRecordRxReadError(bus_number);
		CanStreamPostEvent(bus_number,
				(uint8_t) CAN_STREAM_EVENT_RX_READ_ERROR,
				CAN_STREAM_FRAME_ID_UNKNOWN, 0U, 0U, timestamp);
		return;
	}
	CAN_DiagnosticsRecordRxFrame(bus_number);

	/*
	 * The record format covers standard data frames only, by decision recorded
	 * in the requirements. A frame outside that scope is never silently dropped:
	 * it is counted and reported as an event, because its presence on a bus
	 * configured for CAN 2.0A is itself worth detecting.
	 *
	 * With CAN_ACCEPT_EXTENDED_AS_STANDARD an extended frame whose identifier
	 * still fits in 11 bits is carried as a standard frame instead of being
	 * rejected. This exists because the bench transmitter emits 29-bit frames
	 * only. The count of such arrivals stays visible in the extended_frames
	 * counter, so a trace never hides the fact that the traffic was extended on
	 * the wire, but the identifier value itself becomes indistinguishable from a
	 * genuine standard frame with the same number.
	 */
	if (header.IDE != CAN_ID_STD) {
		CAN_DiagnosticsRecordInvalidFrame(bus_number,
				CAN_DIAG_INVALID_EXTENDED);

#if CAN_ACCEPT_EXTENDED_AS_STANDARD
		if (header.ExtId <= 0x7FFU) {
			static uint8_t remap_announced;

			if (remap_announced == 0U) {
				remap_announced = 1U;
				CanStreamPostEvent(bus_number,
						(uint8_t) CAN_STREAM_EVENT_EXTENDED_REMAPPED,
						(uint16_t) header.ExtId, header.ExtId, 0U,
						timestamp);
			}
			header.StdId = header.ExtId;
		} else {
			CanStreamPostEvent(bus_number,
					(uint8_t) CAN_STREAM_EVENT_EXTENDED_REJECTED,
					CAN_STREAM_FRAME_ID_UNKNOWN, header.ExtId, 0U,
					timestamp);
			return;
		}
#else
		CanStreamPostEvent(bus_number,
				(uint8_t) CAN_STREAM_EVENT_EXTENDED_REJECTED,
				CAN_STREAM_FRAME_ID_UNKNOWN, header.ExtId, 0U, timestamp);
		return;
#endif
	}
	if (header.RTR != CAN_RTR_DATA) {
		CAN_DiagnosticsRecordInvalidFrame(bus_number,
				CAN_DIAG_INVALID_REMOTE);
		CanStreamPostEvent(bus_number,
				(uint8_t) CAN_STREAM_EVENT_REMOTE_REJECTED,
				(uint16_t) header.StdId, 0U, 0U, timestamp);
		return;
	}
	if (header.DLC > sizeof(frame.data)) {
		CAN_DiagnosticsRecordInvalidFrame(bus_number, CAN_DIAG_INVALID_DLC);
		CanStreamPostEvent(bus_number, (uint8_t) CAN_STREAM_EVENT_INVALID_DLC,
				(uint16_t) header.StdId, header.DLC, 0U, timestamp);
		return;
	}
	CAN_DiagnosticsRecordValidFrame(bus_number, header.DLC);

	frame.timestamp = timestamp;
	frame.id = header.StdId;
	frame.can = bus_number;
	frame.dlc = (uint8_t) header.DLC;
	if (WriteToBuffer(&buf, &frame) == 0) {
		CAN_DiagnosticsRecordBufferedFrame(bus_number);
	} else {
		CAN_DiagnosticsRecordRingDrop(bus_number, frame.id, frame.timestamp);
		CanStreamPostEvent(bus_number, (uint8_t) CAN_STREAM_EVENT_RING_DROP,
				(uint16_t) frame.id, 0U, 0U, frame.timestamp);
	}
}

void HAL_CAN_RxFifo0MsgPendingCallback(CAN_HandleTypeDef *hcan) {
	if (hcan == &hcan1) {
		CAN_DiagnosticsRecordFifoLevel(1U,
				HAL_CAN_GetRxFifoFillLevel(hcan, CAN_RX_FIFO0));
		while (HAL_CAN_GetRxFifoFillLevel(hcan, CAN_RX_FIFO0) > 0U) {
			CAN_ReceiveFrame(hcan, CAN_RX_FIFO0, 1U);
		}
	}
}

void HAL_CAN_RxFifo1MsgPendingCallback(CAN_HandleTypeDef *hcan) {
	if (hcan == &hcan2) {
		CAN_DiagnosticsRecordFifoLevel(2U,
				HAL_CAN_GetRxFifoFillLevel(hcan, CAN_RX_FIFO1));
		while (HAL_CAN_GetRxFifoFillLevel(hcan, CAN_RX_FIFO1) > 0U) {
			CAN_ReceiveFrame(hcan, CAN_RX_FIFO1, 2U);
		}
	}
}

void HAL_CAN_RxFifo0FullCallback(CAN_HandleTypeDef *hcan) {
	if (hcan == &hcan1) {
		CAN_DiagnosticsRecordFifoFull(1U);
		CanStreamPostEvent(1U, (uint8_t) CAN_STREAM_EVENT_FIFO_FULL,
				CAN_STREAM_FRAME_ID_UNKNOWN, 0U, 0U, GETTICK);
	}
}

void HAL_CAN_RxFifo1FullCallback(CAN_HandleTypeDef *hcan) {
	if (hcan == &hcan2) {
		CAN_DiagnosticsRecordFifoFull(2U);
		CanStreamPostEvent(2U, (uint8_t) CAN_STREAM_EVENT_FIFO_FULL,
				CAN_STREAM_FRAME_ID_UNKNOWN, 0U, 0U, GETTICK);
	}
}

void HAL_CAN_ErrorCallback(CAN_HandleTypeDef *hcan) {
	uint8_t bus_number;
	uint32_t error_code;

	if (hcan == &hcan1) {
		bus_number = 1U;
	} else if (hcan == &hcan2) {
		bus_number = 2U;
	} else {
		return;
	}

	error_code = HAL_CAN_GetError(hcan);
	{
		uint64_t timestamp = GETTICK;
		CAN_DiagnosticsSnapshot_t snapshot;
		uint8_t state = 0U;
		uint8_t reason = (uint8_t) CAN_STREAM_EVENT_CONTROLLER_ERROR;

		CAN_DiagnosticsRecordError(bus_number, error_code,
				hcan->Instance->ESR, timestamp);
		if (CAN_DiagnosticsGetSnapshot(bus_number, &snapshot) != 0U) {
			state = (uint8_t) snapshot.state;
		}
		if ((error_code
				& (HAL_CAN_ERROR_RX_FOV0 | HAL_CAN_ERROR_RX_FOV1)) != 0U) {
			reason = (uint8_t) CAN_STREAM_EVENT_FIFO_OVERRUN;
		} else if ((error_code
				& (HAL_CAN_ERROR_EWG | HAL_CAN_ERROR_EPV | HAL_CAN_ERROR_BOF))
				!= 0U) {
			reason = (uint8_t) CAN_STREAM_EVENT_STATE_CHANGE;
		}
		CanStreamPostEvent(bus_number, reason, CAN_STREAM_FRAME_ID_UNKNOWN,
				error_code, state, timestamp);
	}
	(void) HAL_CAN_ResetError(hcan);
}

void USB_Error_Handler(void) {
#ifdef LOGGER_DEVELOP
	Error_Handler();
#endif
	textlen_uartdebug = sprintf(text_uartdebug, "USB ERROR HANDLER \n\r");
	HAL_UART_Transmit_IT(&huart_debug, (uint8_t*) text_uartdebug,
			textlen_uartdebug);
	penstate = DEINIT;
}

void printStartTime() {
	uint8_t h = GetStartHours();
	uint8_t min = GetStartMinutes();
	uint8_t s = GetStartSeconds();
	uint8_t y = GetStartYear();
	uint8_t m = GetStartMonth();
	uint8_t d = GetStartDay();

	textlen_uartdebug = sprintf(text_uartdebug,
			"Current date: %02d %02d %02d , %02d:%02d:%02d\r\n", d, m, y, h,
			min, s);
	HAL_UART_Transmit_IT(&huart_debug, (uint8_t*) text_uartdebug,
			textlen_uartdebug);

}

void AppInit(void) {

	if (HAL_GPIO_ReadPin(LED2_IN_GPIO_Port, LED2_IN_Pin) == GPIO_PIN_RESET) {
		MX_CAN1_Init_500k();
		MX_CAN2_Init_500k();
	}

	penstate = DISCONNECTED;
	measurement_mode = 0U;

	ClearBuffer(&buf);
	CAN_DiagnosticsInit();

	RTCInit();

	/*
	 * Order kept exactly as in the version before the stream was added, so that
	 * bringing up the controllers cannot depend on anything new. Record ordering
	 * does not need the stream to start first: frames only reach the serialiser
	 * from the main loop, which begins after this function returns.
	 */
	CANInit();

	HAL_Delay(10);

	if (UARTDMA_Init(&huartdma, &huart_debug) != HAL_OK) {
		Error_Handler();
	}

	CanStreamInit();

	/*
	 * Build marker. Without it there is no way to tell from a log which binary
	 * is actually running on the board, which already cost one round of
	 * diagnostics.
	 */
	{
		static char build[80];
		int written = snprintf(build, sizeof(build),
				"#FWBUILD date=" __DATE__ " time=" __TIME__ " selftest=%u",
				(unsigned int) CAN_SELFTEST_ON_BOOT);
		if (written > 0) {
			printk(build);
			(void) HAL_UART_Transmit(&huart_debug, (uint8_t*) build,
					(uint16_t) written, 100U);
			(void) HAL_UART_Transmit(&huart_debug, (uint8_t*) "\r\n", 2U,
					100U);
		}
	}

	printStartTime();

	return;
}
