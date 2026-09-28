#ifndef INC_RTC_H_
#define INC_RTC_H_

#include "main.h"

extern RTC_HandleTypeDef hrtc;
extern TIM_HandleTypeDef htim2;

uint64_t gettime(uint64_t tick);
uint64_t RTCGetTickUs(void);
uint8_t RTCGetDateTime(RTC_DateTypeDef *date, RTC_TimeTypeDef *time);

#define GETTICK	(RTCGetTickUs())
#define GETTIME (gettime(GETTICK))

void RTCSetDate(uint16_t year, uint8_t month, uint8_t day);
void RTCSetTime(uint8_t hours, uint8_t minutes);
void RTCInit(void);
uint8_t GetStartHours(void);
uint8_t GetStartMinutes(void);
uint8_t GetStartSeconds(void);
uint8_t GetStartYear(void);
uint8_t GetStartMonth(void);
uint8_t GetStartDay(void);
uint64_t GetStartTime_us(void);

#endif /* INC_RTC_H_ */
