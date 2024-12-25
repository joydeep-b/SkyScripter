#include <signal.h>

#include "libindi/indiccd.h"
#include "libindi/basedevice.h"
#include "libindi/baseclient.h"

#include "gflags/gflags.h"

#include <string>
#include <iostream>

using std::string;

DEFINE_string(server, "localhost", "INDI server hostname");
DEFINE_int32(port, 7624, "INDI server port");
DEFINE_string(device, "QHY CCD QHY268M-b93fd94", "INDI device name");

void PrintPropValue(INDI::Property prop) {
  const string prop_name = prop.getName();
  const string dev_name = prop.getBaseDevice().getDeviceName();
  const string base = dev_name + "." + prop_name;
  switch (prop.getType()) {
    case INDI_SWITCH: {
      INDI::PropertyViewSwitch* svp = prop.getSwitch();
      int num_switches = svp->nsp;
      for (int i = 0; i < num_switches; i++) {
        INDI::WidgetViewSwitch* wvs = svp->at(i);
        printf("[S] %s.%-20s.%-20s = %s\n",
                dev_name.c_str(),
                prop_name.c_str(),
                wvs->name,
                (wvs->s == ISS_ON) ? "ON" : "OFF");
      }
      break;
    }
    case INDI_NUMBER: {
      INDI::PropertyViewNumber* nvp = prop.getNumber();
      int num_numbers = nvp->nnp;
      for (int i = 0; i < num_numbers; i++) {
        INDI::WidgetViewNumber* wvn = nvp->at(i);
        printf("[N] %s.%-20s.%-20s = %f\n",
                dev_name.c_str(),
                prop_name.c_str(),
                wvn->name,
                wvn->value);
      }
      break;
    }
    case INDI_TEXT: {
      INDI::PropertyViewText* tvp = prop.getText();
      int num_text = tvp->ntp;
      for (int i = 0; i < num_text; i++) {
        INDI::WidgetViewText* wvt = tvp->at(i);
        printf("[T] %s.%-20s.%-20s = %s\n",
                dev_name.c_str(),
                prop_name.c_str(),
                wvt->name,
                wvt->text);
      }
      break;
    }
    case INDI_LIGHT: {
      INDI::PropertyViewLight* lvp = prop.getLight();
      printf("[L] %s.%-20s.%-20s = [LIGHT]\n",
              dev_name.c_str(),
              prop_name.c_str(),
              lvp->name);
      break;
    }
    case INDI_BLOB: {
      // INDI::setblobMode(B_ALSO, base.c_str());
      INDI::PropertyViewBlob* bvp = prop.getBLOB();
      printf("[B] %s.%-20s.%-20s = [BLOB]\n",
              dev_name.c_str(),
              prop_name.c_str(),
              bvp->name);
      // std::cout << "(BLOB)   " << bvp->getState();
      break;
    }
  }
}


class CameraClient : public INDI::BaseClient {
public:
  void newDevice(INDI::BaseDevice dp) override {
    printf("New device: %s\n", dp.getDeviceName());
    if (strcmp(dp.getDeviceName(), FLAGS_device.c_str()) == 0) {
      printf("Found requested device: %s\n", FLAGS_device.c_str());
      ccdDevice = dp;
    }
  }

  void newProperty(INDI::Property property) override {
    const string prop_name = property.getName();
    const string dev_name = property.getBaseDevice().getDeviceName();
    // PrintPropValue(property);
    if (property.getType() == INDI_BLOB) {
      printf("Setting BLOB mode for %s.%s\n", dev_name.c_str(), prop_name.c_str());
      setBLOBMode(B_ALSO, dev_name.c_str(), prop_name.c_str());
    }
    if (strcmp(property.getName(), "CCD_COOLER_POWER") == 0) {
      INDI::PropertyViewNumber* nvp = property.getNumber();
      INDI::WidgetViewNumber* wvn = nvp->at(0);
      printf("CCD_COOLER_POWER = %5.1f\n", wvn->value);
    } else if (strcmp(property.getName(), "CCD_EXPOSURE") == 0) {
      INDI::PropertyViewNumber* nvp = property.getNumber();
      INDI::WidgetViewNumber* wvn = nvp->at(0);
      printf("CCD_EXPOSURE = %5.1f\n", wvn->value);
    } else if (strcmp(property.getName(), "CCD_TEMPERATURE") == 0) {
      INDI::PropertyViewNumber* nvp = property.getNumber();
      INDI::WidgetViewNumber* wvn = nvp->at(0);
      printf("CCD_TEMPERATURE = %5.1f\n", wvn->value);
    }
  }

  void newBLOB(IBLOB* bp) {
    printf("New BLOB:\n Label=%s\n Name=%s\n Format=%s\n Size=%d\n",
           bp->label, bp->name, bp->format, bp->bloblen);
    printf("Blob length: %d\n", bp->bloblen);
    if (strcmp(bp->name, "CCD1") == 0) {
      FILE *fp = fopen("image.fits", "w");
      fwrite(bp->blob, bp->bloblen, 1, fp);
      fclose(fp);
    }
  }

  void updateProperty(INDI::Property property) override {
    // printf("Update property: %s\n", property.getName());
    if (strcmp(property.getName(), "CCD_COOLER_POWER") == 0) {
      INDI::PropertyViewNumber* nvp = property.getNumber();
      INDI::WidgetViewNumber* wvn = nvp->at(0);
      printf("CCD_COOLER_POWER = %5.1f\n", wvn->value);
    }
    if (strcmp(property.getName(), "CCD_EXPOSURE") == 0) {
      INDI::PropertyViewNumber* nvp = property.getNumber();
      INDI::WidgetViewNumber* wvn = nvp->at(0);
      printf("CCD_EXPOSURE = %5.1f\n", wvn->value);
    }
    if (strcmp(property.getName(), "CCD_TEMPERATURE") == 0) {
      INDI::PropertyViewNumber* nvp = property.getNumber();
      INDI::WidgetViewNumber* wvn = nvp->at(0);
      printf("CCD_TEMPERATURE = %5.1f\n", wvn->value);
    }

    if (property.getType() == INDI_BLOB) {
      INDI::PropertyViewBlob* bvp = property.getBLOB();
      printf("New BLOB:\n Label=%s\n Name=%s\n Label=%s\n Group=%s\n",
           bvp->label, bvp->name, bvp->label, bvp->group);
      if (strcmp(bvp->name, "CCD1") == 0) {
        printf("Received CCD1 BLOB\n");
        const int num_blobs = bvp->nbp;
        for (int i = 0; i < num_blobs; i++) {
          INDI::WidgetViewBlob* bp = bvp->at(i);
          printf("Blob length: %d\n", bp->bloblen);
          // Save the file as image_001.fits, image_002.fits, etc.
          char filename[256];
          snprintf(filename, sizeof(filename), "image_%03d.fits", i);
          printf("Saving to %s\n", filename);
          FILE *fp = fopen(filename, "w");
          fwrite(bp->blob, bp->bloblen, 1, fp);
          fclose(fp);
        }
      }
    }
  }

private:
  INDI::BaseDevice ccdDevice;
};

int main(int argc, char** argv) {
  gflags::ParseCommandLineFlags(&argc, &argv, true);
  CameraClient client;
  client.setServer(FLAGS_server.c_str(), FLAGS_port);
  client.setBLOBMode(B_ALSO, FLAGS_device.c_str());
  client.watchDevice(FLAGS_device.c_str());
  client.connectServer();
  printf("Connected to INDI server %s:%d\n", FLAGS_server.c_str(), FLAGS_port);
  for (int i = 0; i < 10; i++) {
    sleep(1);
  }
  return 0;
}