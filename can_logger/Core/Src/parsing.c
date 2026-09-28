/*
 * parsing.c
 *
 *  Created on: Aug 14, 2024
 *      Author: kubak
 */
#include "parsing.h"
#include "rtc.h"

#include <string.h>

void uint64_tToString(uint64_t num, char *str) {
	for (size_t i = 11U; i > 0U; i--) {
		str[i - 1U] = (char) ('0' + (num % 10U));
		num /= 10U;
	}
}

void uint16_tToHexString(uint16_t num, char *str) {
	static const char hex[] = "0123456789ABCDEF";

	for (size_t i = 4U; i > 0U; i--) {
		str[i - 1U] = hex[num & 0x0FU];
		num >>= 4U;
	}
}

void CanDataToHexString(uint64_t num, char *str) {
	static const char hex[] = "0123456789ABCDEF";

	for (size_t i = 16U; i > 0U; i--) {
		str[i - 1U] = hex[num & 0x0FU];
		num >>= 4U;
	}
}

uint8_t ParsePenFrame(const struct canframe *frame, char *text) {
	uint64_t data;
	uint64_t time;
	size_t data_digits;
	size_t position;

	if ((frame == NULL) || (text == NULL) || (frame->dlc > sizeof(frame->data))) {
		return 0U;
	}

	time = gettime(frame->timestamp);
	memcpy(&data, frame->data, sizeof(data));

	uint64_tToString(time, &text[0]);
	text[11] = ' ';
	uint16_tToHexString((uint16_t) frame->id, &text[12]);
	text[16] = ' ';

	/*
	 * Keep the original four-column format so legacy parsers remain usable.
	 * The v2 DLC is encoded by the number of data hex digits. A zero-length
	 * frame uses the single token "0" to keep the data column present.
	 */
	position = 17U;
	if (frame->dlc == 0U) {
		text[position++] = '0';
	} else {
		data_digits = (size_t) frame->dlc * 2U;
		for (size_t i = data_digits; i > 0U; i--) {
			static const char hex[] = "0123456789ABCDEF";
			text[position + i - 1U] = hex[data & 0x0FU];
			data >>= 4U;
		}
		position += data_digits;
	}
	text[position++] = ' ';
	text[position++] = (char) (frame->can + '0');
	text[position++] = '\r';
	text[position++] = '\n';

	return (uint8_t) position;
}
