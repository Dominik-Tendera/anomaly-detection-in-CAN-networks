/*
 * App.h
 *
 *  Created on: May 10, 2023
 *      Author: kubak
 */

#ifndef INC_APP_H_
#define INC_APP_H_

#define huart_log	huart1
#define huart_debug	huart2

#include <stdint.h>

void AppInit(void);
void USB_Error_Handler(void);
void AppRun(void);

/* Ring buffer observability, consumed by the logger statistics record. */
uint16_t AppGetRingPeak(void);
uint32_t AppGetRingDropTotal(void);
uint8_t AppIsMeasurementMode(void);

#endif /* INC_APP_H_ */
