/*
 * Copyright (c) 2013-2014 Wind River Systems, Inc.
 *
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * @file
 * @brief ARM Cortex-M power management
 *
 */

#include <offsets_short.h>
#include <toolchain.h>
#include <linker/sections.h>
#include <arch/cpu.h>
#ifdef CONFIG_TICKLESS_IDLE
#include <kernel_structs.h>
#endif

_ASM_FILE_PROLOGUE

GTEXT(z_CpuIdleInit)
GTEXT(z_arch_cpu_idle)
GTEXT(z_arch_cpu_atomic_idle)

#if defined(CONFIG_CPU_CORTEX_M)
#define _SCB_SCR 0xE000ED10

#define _SCB_SCR_SEVONPEND (1 << 4)
#define _SCB_SCR_SLEEPDEEP (1 << 2)
#define _SCB_SCR_SLEEPONEXIT (1 << 1)
#define _SCR_INIT_BITS _SCB_SCR_SEVONPEND
#endif

/**
 *
 * @brief Initialization of CPU idle
 *
 * Only called by z_arch_kernel_init(). Sets SEVONPEND bit once for the system's
 * duration.
 *
 * @return N/A
 *
 * C function prototype:
 *
 * void z_CpuIdleInit (void);
 */

SECTION_FUNC(TEXT, z_CpuIdleInit)
#if defined(CONFIG_CPU_CORTEX_M)
	ldr r1, =_SCB_SCR
	movs.n r2, #_SCR_INIT_BITS
	str r2, [r1]
#endif
	bx lr

/**
 *
 * @brief Power save idle routine for ARM Cortex-M
 *
 * This function will be called by the kernel idle loop or possibly within
 * an implementation of _sys_power_save_idle in the kernel when the
 * '_sys_power_save_flag' variable is non-zero.  The ARM 'wfi' instruction
 * will be issued, causing a low-power consumption sleep mode.
 *
 * @return N/A
 *
 * C function prototype:
 *
 * void z_arch_cpu_idle (void);
 */

SECTION_FUNC(TEXT, z_arch_cpu_idle)
#ifdef CONFIG_TRACING
	push {r0, lr}
	bl    sys_trace_idle
#if defined(CONFIG_ARMV6_M_ARMV8_M_BASELINE)
	pop {r0, r1}
        mov lr, r1
#else
	pop {r0, lr}
#endif /* CONFIG_ARMV6_M_ARMV8_M_BASELINE */
#endif /* CONFIG_TRACING */

#if defined(CONFIG_ARMV6_M_ARMV8_M_BASELINE) \
	|| defined(CONFIG_ARMV7_R)
	cpsie i
#elif defined(CONFIG_ARMV7_M_ARMV8_M_MAINLINE)
	/* clear BASEPRI so wfi is awakened by incoming interrupts */
	eors.n r0, r0
	msr BASEPRI, r0
#else
#error Unknown ARM architecture
#endif /* CONFIG_ARMV6_M_ARMV8_M_BASELINE */

	wfi

	bx lr

/**
 *
 * @brief Atomically re-enable interrupts and enter low power mode
 *
 * INTERNAL
 * The requirements for z_arch_cpu_atomic_idle() are as follows:
 * 1) The enablement of interrupts and entering a low-power mode needs to be
 *    atomic, i.e. there should be no period of time where interrupts are
 *    enabled before the processor enters a low-power mode.  See the comments
 *    in k_lifo_get(), for example, of the race condition that occurs
 *    if this requirement is not met.
 *
 * 2) After waking up from the low-power mode, the interrupt lockout state
 *    must be restored as indicated in the 'key' input parameter.
 *
 * @return N/A
 *
 * C function prototype:
 *
 * void z_arch_cpu_atomic_idle (unsigned int key);
 */

SECTION_FUNC(TEXT, z_arch_cpu_atomic_idle)
#ifdef CONFIG_TRACING
	push {r0, lr}
	bl    sys_trace_idle
#if defined(CONFIG_ARMV6_M_ARMV8_M_BASELINE)
        pop {r0, r1}
        mov lr, r1
#else
	pop {r0, lr}
#endif /* CONFIG_ARMV6_M_ARMV8_M_BASELINE */
#endif /* CONFIG_TRACING */

	/*
	 * Lock PRIMASK while sleeping: wfe will still get interrupted by
	 * incoming interrupts but the CPU will not service them right away.
	 */
	cpsid i

	/*
	 * No need to set SEVONPEND, it's set once in z_CpuIdleInit() and never
	 * touched again.
	 */

	/* r0: interrupt mask from caller */

#if defined(CONFIG_ARMV6_M_ARMV8_M_BASELINE) \
	|| defined(CONFIG_ARMV7_R)
	/* No BASEPRI, call wfe directly (SEVONPEND set in z_CpuIdleInit()) */
	wfe

	cmp r0, #0
	bne _irq_disabled
	cpsie i
_irq_disabled:

#elif defined(CONFIG_ARMV7_M_ARMV8_M_MAINLINE)
	/* r1: zero, for setting BASEPRI (needs a register) */
	eors.n r1, r1

	/* unlock BASEPRI so wfe gets interrupted by incoming interrupts */
	msr BASEPRI, r1

	wfe

	msr BASEPRI, r0
	cpsie i
#else
#error Unknown ARM architecture
#endif /* CONFIG_ARMV6_M_ARMV8_M_BASELINE */
	bx lr
