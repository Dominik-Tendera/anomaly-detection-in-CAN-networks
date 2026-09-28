/*
 * can_stream.h
 *
 * Transport of the binary CAN record stream towards the Raspberry Pi over
 * USART1 with DMA. The wire format itself lives in can_stream_codec.c, which
 * carries no HAL dependency and is covered by host tests.
 */

#ifndef INC_CAN_STREAM_H_
#define INC_CAN_STREAM_H_

#include "main.h"
#include "can_stream_codec.h"
#include "ring_buffer.h"

/*
 * Default speed of the data channel. USART1 is clocked from APB2 at 100 MHz,
 * so USARTDIV equals 3.125 and the divider is represented exactly, giving zero
 * baud rate error. The PL011 in a Raspberry Pi 4B runs from a 48 MHz reference,
 * where the divider is 1.5, again exact.
 */
#define CAN_STREAM_DEFAULT_BAUDRATE 2000000U

#define CAN_STREAM_MIN_BAUDRATE 9600U
#define CAN_STREAM_MAX_BAUDRATE 4000000U

/*
 * 16 kB of transmit buffer absorbs about 80 ms of traffic at 2 Mbaud, which
 * covers a blocking write to the USB drive. The exact tolerated stall is a
 * measured quantity, see the session report.
 */
#define CAN_STREAM_TX_BUFFER_SIZE 16384U

/* Upper bound of a single DMA transfer, keeps the completion latency bounded. */
#define CAN_STREAM_DMA_CHUNK_MAX 2048U

#define CAN_STREAM_EVENT_QUEUE_SIZE 32U

/* Minimum spacing between two events of the same class on the same bus. */
#define CAN_STREAM_EVENT_MIN_INTERVAL_MS 10U

#define CAN_STREAM_TIME_SYNC_PERIOD_MS 1000U
#define CAN_STREAM_STATS_PERIOD_MS 100U

/* Command identifiers echoed back in the acknowledgement record. */
typedef enum {
	CAN_STREAM_CMD_NONE = 0,
	CAN_STREAM_CMD_STREAM_ENABLE = 1,
	CAN_STREAM_CMD_MEASUREMENT_MODE = 2,
	CAN_STREAM_CMD_BAUDRATE = 3,
	CAN_STREAM_CMD_RTC_SET = 4,
	CAN_STREAM_CMD_SELFTEST = 5
} CanStreamCommand;

void CanStreamInit(void);

/* Called from the main loop. Drains events, emits periodic records, feeds DMA. */
void CanStreamTask(void);

/* Serialises one CAN frame taken from the ring buffer. Main loop context. */
void CanStreamWriteFrame(const struct canframe *frame);

/* Interrupt safe. Only enqueues, the record is built in the main loop. */
void CanStreamPostEvent(uint8_t bus_number, uint8_t reason, uint16_t frame_id,
		uint32_t error_code, uint8_t state, uint64_t timestamp);

void CanStreamEmitSession(void);
void CanStreamSendAck(uint8_t command, uint8_t status, uint32_t value);

void CanStreamSetEnabled(uint8_t enabled);
uint8_t CanStreamIsEnabled(void);

/* Only mirrors the flag into the reported session state. */
void CanStreamSetMeasurementMode(uint8_t enabled);
void CanStreamSetUsbLogging(uint8_t active);

uint8_t CanStreamSetBaudrate(uint32_t baudrate);
uint32_t CanStreamGetBaudrate(void);

/* Longest observed main loop iteration, fed by the application loop. */
void CanStreamNoteLoopTime(uint32_t microseconds);

/* Human readable counters for the debug channel. */
int CanStreamFormatSummary(char *out, size_t size);

#endif /* INC_CAN_STREAM_H_ */
