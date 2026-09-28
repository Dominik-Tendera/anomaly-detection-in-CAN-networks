/*
 * usb_interface.c
 *
 *  Created on: Aug 14, 2024
 *      Author: kubak
 */
#include "usb_interface.h"
#include "rtc.h"
#include "fatfs.h"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#define USBSTRING_FLUSH_THRESHOLD (1024U * 32U)
#define USBSTRING_CAPACITY (USBSTRING_FLUSH_THRESHOLD + (1024U * 32U))

static FIL *filep;
static char usbString[USBSTRING_CAPACITY];
static uint32_t usbStringIndex;

static uint32_t usbwrite_tick = 0;

static uint8_t USBAppendFormatted(const char *format, ...) {
	size_t available;
	int written;
	va_list args;

	if ((format == NULL) || (usbStringIndex >= USBSTRING_CAPACITY)) {
		return 0U;
	}

	available = USBSTRING_CAPACITY - usbStringIndex;
	va_start(args, format);
	written = vsnprintf(&usbString[usbStringIndex], available, format, args);
	va_end(args);

	if ((written < 0) || ((size_t) written >= available)) {
		return 0U;
	}

	usbStringIndex += (uint32_t) written;
	return 1U;
}

void printk(const char *s) {
	if (s == NULL) {
		return;
	}
	(void) USBAppendFormatted("%llu: %s\r\n",
			(unsigned long long) GETTIME, s);
}

void printk_lld(const char *s, uint64_t d) {
	if (s == NULL) {
		return;
	}
	(void) USBAppendFormatted("%llu: %s %llu\r\n",
			(unsigned long long) GETTIME, s, (unsigned long long) d);
}

uint8_t USBCheckBufferTickSend(void) {
	return ((HAL_GetTick() - usbwrite_tick) >= 500U) ? 1U : 0U;
}

uint8_t IsUSBBufferFull(void) {
	return (usbStringIndex >= USBSTRING_FLUSH_THRESHOLD) ? 1U : 0U;
}

uint8_t USBSyncStrBuffer(void) {
	UINT bytes_to_write;
	UINT bytes_written = 0U;
	FRESULT write_result;
	FRESULT sync_result;

	usbwrite_tick = HAL_GetTick();

	if (usbStringIndex == 0U) {
		return 1U;
	}
	if (filep == NULL) {
		return 0U;
	}

	bytes_to_write = (UINT) usbStringIndex;
	write_result = f_write(filep, usbString, bytes_to_write, &bytes_written);

	if (bytes_written <= usbStringIndex) {
		uint32_t remaining = usbStringIndex - bytes_written;
		if ((bytes_written > 0U) && (remaining > 0U)) {
			memmove(usbString, &usbString[bytes_written], remaining);
		}
		usbStringIndex = remaining;
	} else {
		usbStringIndex = 0U;
		return 0U;
	}

	if ((write_result != FR_OK) || (bytes_written != bytes_to_write)) {
		return 0U;
	}

	sync_result = f_sync(filep);
	HAL_GPIO_WritePin(LED1_GPIO_Port, LED1_Pin, 0);
	return (sync_result == FR_OK) ? 1U : 0U;
}

uint8_t USBWriteStr2Buffer(const char *data, size_t len) {
	if ((data == NULL) || (len > (USBSTRING_CAPACITY - usbStringIndex))) {
		return 0U;
	}

	memcpy(&usbString[usbStringIndex], data, len);
	usbStringIndex += (uint32_t) len;
	return 1U;
}

uint8_t USBMount(void) {
	if (retUSBH != 0U) {
		return 0U;
	}
	return (f_mount(&USBHFatFS, USBHPath, 1U) == FR_OK) ? 1U : 0U;
}

void USBUnmount(void) {
	(void) f_mount(NULL, USBHPath, 0U);
}

uint8_t USBNewDescriptor(const char *filename) {
	if (filename == NULL) {
		return 0U;
	}

	if (filep != NULL) {
		if (USBSyncStrBuffer() == 0U) {
			return 0U;
		}
		if (f_close(filep) != FR_OK) {
			filep = NULL;
			return 0U;
		}
		filep = NULL;
	}

	if (f_open(&USBHFile, filename, FA_OPEN_APPEND | FA_WRITE) != FR_OK) {
		return 0U;
	}

	filep = &USBHFile;
	return 1U;
}

void USBCloseDescriptor(void) {
	if (filep != NULL) {
		(void) f_close(filep);
		filep = NULL;
	}
	usbStringIndex = 0U;
}
