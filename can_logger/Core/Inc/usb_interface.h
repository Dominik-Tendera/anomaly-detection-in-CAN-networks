/*
 * usb_interface.h
 *
 *  Created on: Aug 14, 2024
 *      Author: kubak
 */

#ifndef INC_USB_INTERFACE_H_
#define INC_USB_INTERFACE_H_

#include "main.h"

void printk(const char *s);
void printk_lld(const char *s, uint64_t d);

uint8_t IsUSBBufferFull(void);
uint8_t USBCheckBufferTickSend(void);
uint8_t USBSyncStrBuffer(void);
uint8_t USBMount(void);
void USBUnmount(void);
uint8_t USBNewDescriptor(const char *filename);
void USBCloseDescriptor(void);
uint8_t USBWriteStr2Buffer(const char *data, size_t len);

#endif /* INC_USB_INTERFACE_H_ */
