#ifndef INC_CANINIT_H_
#define INC_CANINIT_H_

#include "main.h"

extern CAN_HandleTypeDef hcan1;
extern CAN_HandleTypeDef hcan2;

void MX_CAN1_Init_500k(void);
void MX_CAN2_Init_500k(void);
void CANInit(void);

/*
 * Self test in silent loopback mode. The controller transmits internally and
 * receives its own frames through the normal interrupt path, without driving the
 * bus lines, so the test is safe with the bus connected and independent of any
 * external node, of the bit rate on the wire and of the termination.
 *
 * A successful run proves the whole receive chain: interrupt, FIFO, ring buffer,
 * record stream and the file on the USB drive. Returns the number of frames
 * handed to the transmit mailboxes, 0 on a configuration failure.
 */
uint8_t CANSelfTest(uint8_t bus_number, uint16_t base_id, uint8_t frame_count);

#endif /* INC_CANINIT_H_ */
