(function () {
    "use strict";

    window.VmbackupModel = {

        deriveModel(data, now) {
            return {
                status: data.status || {},

                discoveredVms: Array.isArray(data.inventory)
                    ? data.inventory
                    : [],

                registeredVms: Array.isArray(data.registeredVms)
                    ? data.registeredVms
                    : [],

                storage: Array.isArray(data.storage)
                    ? data.storage
                    : [],

                jobs: Array.isArray(data.jobs)
                    ? data.jobs
                    : [],

                runs: Array.isArray(data.runPage)
                    ? data.runPage
                    : [],

                recovery: Array.isArray(data.recovery)
                    ? data.recovery
                    : [],

                generatedAt: now,
            };
        },

        validate(model) {
            return Boolean(
                model &&
                typeof model === "object"
            );
        },

    };

    console.log(
        "MODEL READY",
        window.VmbackupModel
    );

})();
