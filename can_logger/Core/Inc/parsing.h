#ifndef INC_PARSING_H_
#define INC_PARSING_H_

#include "main.h"
#include "ring_buffer.h"

uint8_t ParsePenFrame(const struct canframe *frame, char *text);
void uint64_tToString(uint64_t num, char *str);
void uint16_tToHexString(uint16_t num, char *str);
void CanDataToHexString(uint64_t num, char *str);
#endif /* INC_PARSING_H_ */
