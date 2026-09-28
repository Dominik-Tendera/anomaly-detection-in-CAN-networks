#include "caninit.h"

static void CAN_ConfigureHandle(CAN_HandleTypeDef *hcan, CAN_TypeDef *instance,
		uint32_t prescaler) {
	hcan->Instance = instance;
	hcan->Init.Prescaler = prescaler;
	hcan->Init.Mode = CAN_MODE_NORMAL;
	hcan->Init.SyncJumpWidth = CAN_SJW_1TQ;
	hcan->Init.TimeSeg1 = CAN_BS1_8TQ;
	hcan->Init.TimeSeg2 = CAN_BS2_1TQ;
	hcan->Init.TimeTriggeredMode = DISABLE;
	hcan->Init.AutoBusOff = ENABLE;
	hcan->Init.AutoWakeUp = DISABLE;
	hcan->Init.AutoRetransmission = DISABLE;
	hcan->Init.ReceiveFifoLocked = DISABLE;
	hcan->Init.TransmitFifoPriority = DISABLE;

	if (HAL_CAN_Init(hcan) != HAL_OK) {
		Error_Handler();
	}
}

void MX_CAN1_Init_500k(void) {
	CAN_ConfigureHandle(&hcan1, CAN1, 10U);
}

void MX_CAN2_Init_500k(void) {
	CAN_ConfigureHandle(&hcan2, CAN2, 10U);
}

/*
 * Returns 1 on success instead of resetting the microcontroller. The reset is
 * still applied by CANInit at start-up, where an unconfigurable controller is
 * fatal, but the self test must never turn a diagnostic into a boot loop.
 */
static uint8_t CAN_ConfigureFilter(CAN_HandleTypeDef *hcan, uint32_t bank,
		uint32_t fifo) {
	CAN_FilterTypeDef filter = { 0 };

	filter.FilterBank = bank;
	filter.FilterMode = CAN_FILTERMODE_IDMASK;
	filter.FilterScale = CAN_FILTERSCALE_32BIT;
	filter.FilterFIFOAssignment = fifo;
	filter.FilterActivation = ENABLE;
	filter.SlaveStartFilterBank = 14U;

	return (HAL_CAN_ConfigFilter(hcan, &filter) == HAL_OK) ? 1U : 0U;
}

#define CAN_COMMON_NOTIFICATIONS (CAN_IT_ERROR_WARNING \
		| CAN_IT_ERROR_PASSIVE | CAN_IT_BUSOFF \
		| CAN_IT_LAST_ERROR_CODE | CAN_IT_ERROR)

static uint8_t CAN_ConfigureBus(CAN_HandleTypeDef *hcan, uint32_t bank,
		uint32_t fifo, uint32_t notifications) {
	if (CAN_ConfigureFilter(hcan, bank, fifo) == 0U) {
		return 0U;
	}

	if (HAL_CAN_ActivateNotification(hcan,
			notifications | CAN_COMMON_NOTIFICATIONS) != HAL_OK) {
		return 0U;
	}
	if (HAL_CAN_Start(hcan) != HAL_OK) {
		return 0U;
	}
	return 1U;
}

void CANInit(void) {
	if (CAN_ConfigureBus(&hcan1, 0U, CAN_RX_FIFO0,
			CAN_IT_RX_FIFO0_MSG_PENDING | CAN_IT_RX_FIFO0_FULL
			| CAN_IT_RX_FIFO0_OVERRUN) == 0U) {
		Error_Handler();
	}
	if (CAN_ConfigureBus(&hcan2, 14U, CAN_RX_FIFO1,
			CAN_IT_RX_FIFO1_MSG_PENDING | CAN_IT_RX_FIFO1_FULL
			| CAN_IT_RX_FIFO1_OVERRUN) == 0U) {
		Error_Handler();
	}
}

/*
 * Reconfigures one controller with a different mode, keeping the bit timing
 * untouched, and brings the filter, the notifications and the controller back
 * up. Returns 1 on success.
 */
static uint8_t CAN_ApplyMode(CAN_HandleTypeDef *hcan, uint32_t mode) {
	uint32_t bank;
	uint32_t fifo;
	uint32_t rx_notifications;

	if (hcan == &hcan1) {
		bank = 0U;
		fifo = CAN_RX_FIFO0;
		rx_notifications = CAN_IT_RX_FIFO0_MSG_PENDING | CAN_IT_RX_FIFO0_FULL
				| CAN_IT_RX_FIFO0_OVERRUN;
	} else if (hcan == &hcan2) {
		bank = 14U;
		fifo = CAN_RX_FIFO1;
		rx_notifications = CAN_IT_RX_FIFO1_MSG_PENDING | CAN_IT_RX_FIFO1_FULL
				| CAN_IT_RX_FIFO1_OVERRUN;
	} else {
		return 0U;
	}

	if (HAL_CAN_Stop(hcan) != HAL_OK) {
		return 0U;
	}
	hcan->Init.Mode = mode;
	if (HAL_CAN_Init(hcan) != HAL_OK) {
		return 0U;
	}
	return CAN_ConfigureBus(hcan, bank, fifo, rx_notifications);
}

uint8_t CANSelfTest(uint8_t bus_number, uint16_t base_id, uint8_t frame_count) {
	CAN_HandleTypeDef *hcan;
	uint8_t queued = 0U;

	if (bus_number == 1U) {
		hcan = &hcan1;
	} else if (bus_number == 2U) {
		hcan = &hcan2;
	} else {
		return 0U;
	}
	if ((base_id > 0x7FFU) || (frame_count == 0U)) {
		return 0U;
	}

	if (CAN_ApplyMode(hcan, CAN_MODE_SILENT_LOOPBACK) == 0U) {
		(void) CAN_ApplyMode(hcan, CAN_MODE_NORMAL);
		return 0U;
	}

	for (uint8_t i = 0U; i < frame_count; i++) {
		CAN_TxHeaderTypeDef header = { 0 };
		uint8_t payload[8];
		uint32_t mailbox;
		uint32_t deadline;

		header.StdId = (uint32_t) ((base_id + i) & 0x7FFU);
		header.IDE = CAN_ID_STD;
		header.RTR = CAN_RTR_DATA;
		header.DLC = (uint32_t) (i % 9U); /* sweeps DLC 0 to 8 */
		header.TransmitGlobalTime = DISABLE;
		for (uint8_t byte = 0U; byte < 8U; byte++) {
			payload[byte] = (uint8_t) (0xA0U + byte + i);
		}

		/* Bounded wait for a free mailbox, the loopback drains quickly. */
		deadline = HAL_GetTick() + 10U;
		while ((HAL_CAN_GetTxMailboxesFreeLevel(hcan) == 0U)
				&& ((int32_t) (HAL_GetTick() - deadline) < 0)) {
			__NOP();
		}
		if (HAL_CAN_AddTxMessage(hcan, &header, payload, &mailbox) != HAL_OK) {
			break;
		}
		queued++;
	}

	/* Let the loopback deliver everything before the mode goes back. */
	HAL_Delay(5U);

	/*
	 * Deliberately no Error_Handler here. This runs as a diagnostic, and a reset
	 * on a failed mode restoration would turn a diagnostic into a boot loop. The
	 * caller can read the resulting state from the register dump.
	 */
	(void) CAN_ApplyMode(hcan, CAN_MODE_NORMAL);

	return queued;
}
