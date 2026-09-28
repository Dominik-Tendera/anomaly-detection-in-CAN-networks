/*
 * ring_buffer.h
 *
 *  Created on: Dec 18, 2023
 *      Author: kubak
 */

#ifndef INC_RING_BUFFER_H_
#define INC_RING_BUFFER_H_

#include "main.h"

#define BUFFER_SIZE (1024*5)

struct canframe {
	uint64_t timestamp;
	uint32_t id;
	uint8_t data[8];
	uint8_t can;
	uint8_t dlc;
};

_Static_assert(sizeof(struct canframe) == 24U,
		"CAN frame layout must stay within the RAM budget");

typedef struct {
	struct canframe CANFrames[BUFFER_SIZE];
	volatile uint16_t head;
	volatile uint16_t tail;
	volatile uint32_t missed_data;
	volatile uint16_t len;
	volatile uint16_t lenmax;
} RingBuffer_t;

void ClearBuffer(RingBuffer_t *Buffer);

int8_t WriteToBuffer(RingBuffer_t *Buffer, const struct canframe *Data);

int8_t ReadFromBuffer(RingBuffer_t *Buffer, struct canframe *Data);

#endif /* INC_RING_BUFFER_H_ */
