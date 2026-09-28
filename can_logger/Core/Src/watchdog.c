#include "watchdog.h"
#include "main.h"

#define IWDG_KEY_ENABLE 0xCCCCU
#define IWDG_KEY_RELOAD 0xAAAAU
#define IWDG_KEY_WRITE_ACCESS 0x5555U
#define IWDG_RELOAD_MAX 0x0FFFU
#define IWDG_REGISTER_UPDATE_TIMEOUT_MS 100U

void WatchdogInit(void) {
#ifndef LOGGER_DEVELOP
	uint32_t start_tick;

	/*
	 * LSI / 256 with the maximum reload gives at least about 22 seconds
	 * at the fastest specified LSI. This leaves margin for a slow USB sync.
	 */
	IWDG->KR = IWDG_KEY_ENABLE;
	IWDG->KR = IWDG_KEY_WRITE_ACCESS;
	IWDG->PR = IWDG_PR_PR_1 | IWDG_PR_PR_2;
	IWDG->RLR = IWDG_RELOAD_MAX;

	start_tick = HAL_GetTick();
	while (IWDG->SR != 0U) {
		if ((HAL_GetTick() - start_tick)
				>= IWDG_REGISTER_UPDATE_TIMEOUT_MS) {
			NVIC_SystemReset();
		}
	}
	IWDG->KR = IWDG_KEY_RELOAD;
#endif
}

void WatchdogRefresh(void) {
#ifndef LOGGER_DEVELOP
	IWDG->KR = IWDG_KEY_RELOAD;
#endif
}
