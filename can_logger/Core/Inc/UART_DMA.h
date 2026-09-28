#ifndef INC_UART_DMA_H_
#define INC_UART_DMA_H_

#include "stm32f4xx_hal.h"

#define UARTDMA_RX_BUFFER_SIZE 64U

typedef struct {
	UART_HandleTypeDef *huart;
	uint8_t DMA_RX_Buffer[UARTDMA_RX_BUFFER_SIZE];
	char Line_Buffer[UARTDMA_RX_BUFFER_SIZE];
	uint16_t UartBufferTail;
	uint16_t LineBufferPos;
	uint8_t DiscardUntilEol;
} UARTDMA_HandleTypeDef;

HAL_StatusTypeDef UARTDMA_Init(UARTDMA_HandleTypeDef *huartdma,
		UART_HandleTypeDef *huart);
uint16_t UARTDMA_GetDataCount(UARTDMA_HandleTypeDef *huartdma);
int UARTDMA_GetCharFromBuffer(UARTDMA_HandleTypeDef *huartdma);
int8_t UARTDMA_GetLineFromBuffer(UARTDMA_HandleTypeDef *huartdma);

#endif /* INC_UART_DMA_H_ */
