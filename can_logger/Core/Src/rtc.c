/*
 * rtc.c
 *
 *  Created on: Aug 14, 2024
 *      Author: kubak
 */

#include "rtc.h"
#include "App.h"

#include <stdio.h>
#include <string.h>

#define MICROSECONDS_PER_DAY (24ULL * 60ULL * 60ULL * 1000000ULL)

extern UART_HandleTypeDef huart2;

static RTC_TimeTypeDef RtcStartTime;
static RTC_DateTypeDef RtcStartDate;
static uint64_t RtcStartTimeUs;
static volatile uint32_t TimerOverflowCount;

uint64_t gettime(uint64_t tick) {
	return (RtcStartTimeUs + tick) % MICROSECONDS_PER_DAY;
}

uint64_t RTCGetTickUs(void) {
	uint32_t primask = __get_PRIMASK();
	uint32_t overflows;
	uint32_t counter;

	__disable_irq();
	overflows = TimerOverflowCount;
	counter = __HAL_TIM_GET_COUNTER(&htim2);
	if ((__HAL_TIM_GET_FLAG(&htim2, TIM_FLAG_UPDATE) != RESET)
			&& (__HAL_TIM_GET_IT_SOURCE(&htim2, TIM_IT_UPDATE) != RESET)) {
		overflows++;
	}
	if (primask == 0U) {
		__enable_irq();
	}

	return ((uint64_t) overflows << 32U) | counter;
}

uint8_t RTCGetDateTime(RTC_DateTypeDef *date, RTC_TimeTypeDef *time) {
	if ((date == NULL) || (time == NULL)) {
		return 0U;
	}
	if (HAL_RTC_GetTime(&hrtc, time, RTC_FORMAT_BIN) != HAL_OK) {
		return 0U;
	}
	if (HAL_RTC_GetDate(&hrtc, date, RTC_FORMAT_BIN) != HAL_OK) {
		return 0U;
	}
	return 1U;
}

static uint8_t RTCWeekday(uint16_t year, uint8_t month, uint8_t day) {
	static const uint8_t month_offset[] = {
			0U, 3U, 2U, 5U, 0U, 3U, 5U, 1U, 4U, 6U, 2U, 4U
	};
	uint16_t adjusted_year = year;
	uint8_t weekday;

	if (month < 3U) {
		adjusted_year--;
	}
	weekday = (uint8_t) ((adjusted_year + adjusted_year / 4U
			- adjusted_year / 100U + adjusted_year / 400U
			+ month_offset[month - 1U] + day) % 7U);
	return (weekday == 0U) ? RTC_WEEKDAY_SUNDAY : weekday;
}

void RTCSetDate(uint16_t year, uint8_t month, uint8_t day) {
	RTC_DateTypeDef date = { 0 };
	char text[48];
	int length;

	date.WeekDay = RTCWeekday(year, month, day);
	date.Month = month;
	date.Date = day;
	date.Year = (uint8_t) (year - 2000U);

	if (HAL_RTC_SetDate(&hrtc, &date, RTC_FORMAT_BIN) != HAL_OK) {
		Error_Handler();
	}

	length = snprintf(text, sizeof(text),
			"The date %02u %02u %04u has been set.\r\n",
			day, month, year);
	if (length > 0) {
		(void) HAL_UART_Transmit(&huart_debug, (uint8_t*) text,
				(uint16_t) length, 1000U);
	}

	static const uint8_t rebooting[] = "Rebooting...\r\n";
	(void) HAL_UART_Transmit(&huart_debug, (uint8_t*) rebooting,
			(uint16_t) (sizeof(rebooting) - 1U), 1000U);
	HAL_Delay(100U);
	NVIC_SystemReset();
}

void RTCSetTime(uint8_t hours, uint8_t minutes) {
	RTC_TimeTypeDef time = { 0 };
	char text[48];
	int length;

	time.Hours = hours;
	time.Minutes = minutes;
	time.Seconds = 0U;
	time.DayLightSaving = RTC_DAYLIGHTSAVING_NONE;
	time.StoreOperation = RTC_STOREOPERATION_RESET;
	if (HAL_RTC_SetTime(&hrtc, &time, RTC_FORMAT_BIN) != HAL_OK) {
		Error_Handler();
	}

	length = snprintf(text, sizeof(text),
			"The time %02u:%02u has been set.\r\n", hours, minutes);
	if (length > 0) {
		(void) HAL_UART_Transmit(&huart_debug, (uint8_t*) text,
				(uint16_t) length, 1000U);
	}

	static const uint8_t rebooting[] = "Rebooting...\r\n";
	(void) HAL_UART_Transmit(&huart_debug, (uint8_t*) rebooting,
			(uint16_t) (sizeof(rebooting) - 1U), 1000U);
	HAL_Delay(100U);
	NVIC_SystemReset();
}

void RTCInit(void) {
	if (RTCGetDateTime(&RtcStartDate, &RtcStartTime) == 0U) {
		Error_Handler();
	}

	TimerOverflowCount = 0U;
	__HAL_TIM_SET_COUNTER(&htim2, 0U);
	__HAL_TIM_CLEAR_FLAG(&htim2, TIM_FLAG_UPDATE);
	if (HAL_TIM_Base_Start_IT(&htim2) != HAL_OK) {
		Error_Handler();
	}

	RtcStartTimeUs = (uint64_t) RtcStartTime.Hours * 60ULL * 60ULL * 1000000ULL;
	RtcStartTimeUs += (uint64_t) RtcStartTime.Minutes * 60ULL * 1000000ULL;
	RtcStartTimeUs += (uint64_t) RtcStartTime.Seconds * 1000000ULL;
}

void HAL_TIM_PeriodElapsedCallback(TIM_HandleTypeDef *htim) {
	if (htim->Instance == TIM2) {
		TimerOverflowCount++;
	}
}

uint8_t GetStartHours(void) {
	return RtcStartTime.Hours;
}

uint8_t GetStartMinutes(void) {
	return RtcStartTime.Minutes;
}

uint8_t GetStartSeconds(void) {
	return RtcStartTime.Seconds;
}

uint8_t GetStartYear(void) {
	return RtcStartDate.Year;
}

uint8_t GetStartMonth(void) {
	return RtcStartDate.Month;
}

uint8_t GetStartDay(void) {
	return RtcStartDate.Date;
}

uint64_t GetStartTime_us(void) {
	return RtcStartTimeUs;
}
