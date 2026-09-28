#include "ring_buffer.h"

static uint32_t RingBuffer_EnterCritical(void) {
	uint32_t primask = __get_PRIMASK();
	__disable_irq();
	return primask;
}

static void RingBuffer_ExitCritical(uint32_t primask) {
	if (primask == 0U) {
		__enable_irq();
	}
}

void ClearBuffer(RingBuffer_t *Buffer) {
	uint32_t primask = RingBuffer_EnterCritical();

	Buffer->head = 0;
	Buffer->tail = 0;
	Buffer->missed_data = 0;
	Buffer->len = 0;
	Buffer->lenmax = 0;

	RingBuffer_ExitCritical(primask);
}

int8_t WriteToBuffer(RingBuffer_t *Buffer, const struct canframe *Data) {
	uint32_t primask;
	uint16_t next_head;
	uint16_t tail;
	uint16_t len;

	if ((Buffer == NULL) || (Data == NULL)) {
		return -1;
	}

	/* CAN1 and CAN2 both produce data from interrupt context. */
	primask = RingBuffer_EnterCritical();
	tail = Buffer->tail;
	next_head = (uint16_t)((Buffer->head + 1U) % BUFFER_SIZE);

	if (next_head == tail) {
		Buffer->missed_data++;
		RingBuffer_ExitCritical(primask);
		return -1;
	}

	Buffer->CANFrames[Buffer->head] = *Data;
	__DMB();
	Buffer->head = next_head;
	len = (uint16_t)((next_head + BUFFER_SIZE - tail) % BUFFER_SIZE);

	Buffer->len = len;

	if (len > Buffer->lenmax) {
		Buffer->lenmax = len;
	}

	RingBuffer_ExitCritical(primask);

	return 0;
}

int8_t ReadFromBuffer(RingBuffer_t *Buffer, struct canframe *Data) {
	uint16_t tail;
	uint16_t head;

	if ((Buffer == NULL) || (Data == NULL)) {
		return -1;
	}

	tail = Buffer->tail;
	head = Buffer->head;
	if (tail == head) {
		return -1;
	}

	__DMB();
	*Data = Buffer->CANFrames[tail];
	tail = (uint16_t)((tail + 1U) % BUFFER_SIZE);
	Buffer->tail = tail;
	Buffer->len = (uint16_t)((head + BUFFER_SIZE - tail) % BUFFER_SIZE);

	return 0;
}


