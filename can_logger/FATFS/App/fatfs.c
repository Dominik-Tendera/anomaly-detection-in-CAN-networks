/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file   fatfs.c
  * @brief  Code for fatfs applications
  ******************************************************************************
  * @attention
  *
  * Copyright (c) 2023 STMicroelectronics.
  * All rights reserved.
  *
  * This software is licensed under terms that can be found in the LICENSE file
  * in the root directory of this software component.
  * If no LICENSE file comes with this software, it is provided AS-IS.
  *
  ******************************************************************************
  */
/* USER CODE END Header */
#include "fatfs.h"

/* USER CODE BEGIN Includes */
#include "rtc.h"
/* USER CODE END Includes */

uint8_t retUSBH;    /* Return value for USBH */
char USBHPath[4];   /* USBH logical drive path */
FATFS USBHFatFS;    /* File system object for USBH logical drive */
FIL USBHFile;       /* File object for USBH */

/* USER CODE BEGIN Variables */

/* USER CODE END Variables */

void MX_FATFS_Init(void)
{
  /*## FatFS: Link the USBH driver ###########################*/
  retUSBH = FATFS_LinkDriver(&USBH_Driver, USBHPath);

  /* USER CODE BEGIN Init */
  /* additional user code for init */
  /* USER CODE END Init */
}

/**
  * @brief  Gets Time from RTC
  * @param  None
  * @retval Time in DWORD
  */
DWORD get_fattime(void)
{
  /* USER CODE BEGIN get_fattime */
  RTC_TimeTypeDef time = {0};
  RTC_DateTypeDef date = {0};

  if (RTCGetDateTime(&date, &time) == 0U)
  {
    return ((DWORD)40U << 25) | ((DWORD)1U << 21) | ((DWORD)1U << 16);
  }

  return ((DWORD)(date.Year + 20U) << 25)
      | ((DWORD)date.Month << 21)
      | ((DWORD)date.Date << 16)
      | ((DWORD)time.Hours << 11)
      | ((DWORD)time.Minutes << 5)
      | ((DWORD)time.Seconds / 2U);
  /* USER CODE END get_fattime */
}

/* USER CODE BEGIN Application */

/* USER CODE END Application */
