CC ?= cc
CFLAGS ?= -O3 -Wall -Wextra -std=c11
PKG_CONFIG ?= pkg-config

CFITSIO_CFLAGS := $(shell $(PKG_CONFIG) --cflags cfitsio 2>/dev/null)
CFITSIO_LIBS := $(shell $(PKG_CONFIG) --libs cfitsio 2>/dev/null)

UNAME_S := $(shell uname -s)
ifeq ($(UNAME_S),Darwin)
  OMP_CFLAGS ?= -Xpreprocessor -fopenmp -I/opt/homebrew/opt/libomp/include
  OMP_LIBS ?= -L/opt/homebrew/opt/libomp/lib -lomp
else
  OMP_CFLAGS ?= -fopenmp
  OMP_LIBS ?= -fopenmp
endif

TARGETS := local_normalize local_normalize_trimmed_median local_normalize_v2 local_normalize_unregistered
LNC_UNREGISTERED_SRCS := local_normalize_unregistered.c lnc_common.c lnc_fits.c lnc_transform.c lnc_grid.c lnc_fit.c lnc_unregistered_core.c

.PHONY: all clean

all: $(TARGETS)

local_normalize: local_normalize.c
	$(CC) $(CFLAGS) $(OMP_CFLAGS) $(CFITSIO_CFLAGS) -o $@ $< $(CFITSIO_LIBS) $(OMP_LIBS) -lm

local_normalize_trimmed_median: local_normalize_trimmed_median.c local_normalize.c
	$(CC) $(CFLAGS) $(OMP_CFLAGS) $(CFITSIO_CFLAGS) -o $@ $< $(CFITSIO_LIBS) $(OMP_LIBS) -lm

local_normalize_v2: local_normalize_v2.c
	$(CC) $(CFLAGS) $(OMP_CFLAGS) $(CFITSIO_CFLAGS) -o $@ $< $(CFITSIO_LIBS) $(OMP_LIBS) -lm

local_normalize_unregistered: $(LNC_UNREGISTERED_SRCS) lnc_common.h lnc_fits.h lnc_transform.h lnc_grid.h lnc_fit.h lnc_unregistered_core.h
	$(CC) $(CFLAGS) $(OMP_CFLAGS) $(CFITSIO_CFLAGS) -o $@ $(LNC_UNREGISTERED_SRCS) $(CFITSIO_LIBS) $(OMP_LIBS) -lm

clean:
	rm -f $(TARGETS)
