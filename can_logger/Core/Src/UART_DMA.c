#include "UART_DMA.h"

#include <string.h>

HAL_StatusTypeDef UARTDMA_Init(UARTDMA_HandleTypeDef *huartdma,
		UART_HandleTypeDef *huart) {
	if ((huartdma == NULL) || (huart == NULL) || (huart->hdmarx == NULL)) {
		return HAL_ERROR;
	}

	huartdma->huart = huart;
	huartdma->UartBufferTail = 0U;
	huartdma->LineBufferPos = 0U;
	huartdma->DiscardUntilEol = 0U;
	memset(huartdma->DMA_RX_Buffer, 0, sizeof(huartdma->DMA_RX_Buffer));
	memset(huartdma->Line_Buffer, 0, sizeof(huartdma->Line_Buffer));

	return HAL_UART_Receive_DMA(huartdma->huart, huartdma->DMA_RX_Buffer,
			UARTDMA_RX_BUFFER_SIZE);
}

static uint8_t UARTDMA_IsValid(const UARTDMA_HandleTypeDef *huartdma) {
	return ((huartdma != NULL) && (huartdma->huart != NULL)
			&& (huartdma->huart->hdmarx != NULL)) ? 1U : 0U;
}

static uint16_t UARTDMA_GetHead(UARTDMA_HandleTypeDef *huartdma) {
	uint32_t remaining = __HAL_DMA_GET_COUNTER(huartdma->huart->hdmarx);
	return (uint16_t) (UARTDMA_RX_BUFFER_SIZE - remaining);
}

uint16_t UARTDMA_GetDataCount(UARTDMA_HandleTypeDef *huartdma) {
	uint16_t current_head;

	if (UARTDMA_IsValid(huartdma) == 0U) {
		return 0U;
	}

	current_head = UARTDMA_GetHead(huartdma);
	if (current_head >= huartdma->UartBufferTail) {
		return (uint16_t) (current_head - huartdma->UartBufferTail);
	}
	return (uint16_t) (UARTDMA_RX_BUFFER_SIZE - huartdma->UartBufferTail
			+ current_head);
}

int UARTDMA_GetCharFromBuffer(UARTDMA_HandleTypeDef *huartdma) {
	uint16_t current_head;
	uint8_t value;

	if (UARTDMA_IsValid(huartdma) == 0U) {
		return -1;
	}

	current_head = UARTDMA_GetHead(huartdma);
	if (current_head == huartdma->UartBufferTail) {
		return -1;
	}

	value = huartdma->DMA_RX_Buffer[huartdma->UartBufferTail];
	huartdma->UartBufferTail = (uint16_t) ((huartdma->UartBufferTail + 1U)
			% UARTDMA_RX_BUFFER_SIZE);
	return value;
}

int8_t UARTDMA_GetLineFromBuffer(UARTDMA_HandleTypeDef *huartdma) {
	if (UARTDMA_IsValid(huartdma) == 0U) {
		return -1;
	}

	while (1) {
		uint16_t current_head = UARTDMA_GetHead(huartdma);
		uint8_t received;

		if (current_head == huartdma->UartBufferTail) {
			return -1;
		}

		received = huartdma->DMA_RX_Buffer[huartdma->UartBufferTail];
		huartdma->UartBufferTail = (uint16_t) ((huartdma->UartBufferTail + 1U)
				% UARTDMA_RX_BUFFER_SIZE);

		if ((received == '\r') || (received == '\n')) {
			if (huartdma->DiscardUntilEol != 0U) {
				huartdma->DiscardUntilEol = 0U;
				huartdma->LineBufferPos = 0U;
				continue;
			}
			if (huartdma->LineBufferPos == 0U) {
				continue;
			}
			huartdma->Line_Buffer[huartdma->LineBufferPos] = '\0';
			huartdma->LineBufferPos = 0U;
			return 0;
		}

		if (huartdma->DiscardUntilEol != 0U) {
			continue;
		}
		if (huartdma->LineBufferPos >= (UARTDMA_RX_BUFFER_SIZE - 1U)) {
			huartdma->LineBufferPos = 0U;
			huartdma->DiscardUntilEol = 1U;
			continue;
		}

		huartdma->Line_Buffer[huartdma->LineBufferPos++] = (char) received;
	}
}
