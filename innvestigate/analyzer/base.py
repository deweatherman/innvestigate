from __future__ import\
    absolute_import, print_function, division, unicode_literals


###############################################################################
###############################################################################
###############################################################################

###############################################################################
###############################################################################
###############################################################################

import tensorflow.keras.layers as keras_layers
import warnings

import inspect
from .. import utils as iutils
from . import reverse_map
from ..utils.keras import checks as kchecks
from ..utils.keras import graph as kgraph


__all__ = [
    "NotAnalyzeableModelException",
    "AnalyzerBase",

    "TrainerMixin",
    "OneEpochTrainerMixin",

    "AnalyzerNetworkBase",
    "ReverseAnalyzerBase"
]


###############################################################################
###############################################################################
###############################################################################


class NotAnalyzeableModelException(Exception):
    """Indicates that the model cannot be analyzed by an analyzer."""
    pass


class AnalyzerBase(object):
    """ The basic interface of an iNNvestigate analyzer.

    This class defines the basic interface for analyzers:

    >>> model = create_keras_model()
    >>> a = Analyzer(model)
    >>> a.fit(X_train)  # If analyzer needs training.
    >>> analysis = a.analyze(X_test)
    >>>
    >>> state = a.save()
    >>> a_new = A.load(*state)
    >>> analysis = a_new.analyze(X_test)

    :param model: A tf.keras model.
    :param disable_model_checks: Do not execute model checks that enforce
      compatibility of analyzer and model.

    .. note:: To develop a new analyzer derive from
      :class:`AnalyzerNetworkBase`.
    """

    def __init__(self, model, disable_model_checks=False):
        self._model = model
        self._disable_model_checks = disable_model_checks

        self._do_model_checks()

    def _add_model_check(self, check, message, check_type="exception"):
        if getattr(self, "_model_check_done", False):
            raise Exception("Cannot add model check anymore."
                            " Check was already performed.")

        if not hasattr(self, "_model_checks"):
            self._model_checks = []

        check_instance = {
            "check": check,
            "message": message,
            "type": check_type,
        }
        self._model_checks.append(check_instance)

    def _do_model_checks(self):
        model_checks = getattr(self, "_model_checks", [])

        if not self._disable_model_checks and len(model_checks) > 0:
            check = [x["check"] for x in model_checks]
            types = [x["type"] for x in model_checks]
            messages = [x["message"] for x in model_checks]

            checked = kgraph.model_contains(self._model, check)
            tmp = zip(iutils.to_list(checked), messages, types)

            for checked_layers, message, check_type in tmp:
                if len(checked_layers) > 0:
                    tmp_message = ("%s\nCheck triggerd by layers: %s" %
                                   (message, checked_layers))

                    if check_type == "exception":
                        raise NotAnalyzeableModelException(tmp_message)
                    elif check_type == "warning":
                        # TODO(albermax) only the first warning will be shown
                        warnings.warn(tmp_message)
                    else:
                        raise NotImplementedError()

        self._model_check_done = True

    def fit(self, *args, **kwargs):
        """
        Stub that eats arguments. If an analyzer needs training
        include :class:`TrainerMixin`.

        :param disable_no_training_warning: Do not warn if this function is
          called despite no training is needed.
        """
        disable_no_training_warning = kwargs.pop("disable_no_training_warning",
                                                 False)
        if not disable_no_training_warning:
            # issue warning if not training is foreseen,
            # but is fit is still called.
            warnings.warn("This analyzer does not need to be trained."
                          " Still fit() is called.", RuntimeWarning)

    def fit_generator(self, *args, **kwargs):
        """
        Stub that eats arguments. If an analyzer needs training
        include :class:`TrainerMixin`.

        :param disable_no_training_warning: Do not warn if this function is
          called despite no training is needed.
        """
        disable_no_training_warning = kwargs.pop("disable_no_training_warning",
                                                 False)
        if not disable_no_training_warning:
            # issue warning if not training is foreseen,
            # but is fit is still called.
            warnings.warn("This analyzer does not need to be trained."
                          " Still fit_generator() is called.", RuntimeWarning)

    def analyze(self, X):
        """
        Analyze the behavior of model on input `X`.

        :param X: Input as expected by model.
        """
        raise NotImplementedError()


###############################################################################
###############################################################################
###############################################################################


class TrainerMixin(object):
    """Mixin for analyzer that adapt to data.

    This convenience interface exposes a Keras like training routing
    to the user.
    """

    # todo: extend with Y
    def fit(self,
            X=None,
            batch_size=32,
            **kwargs):
        """
        Takes the same parameters as Keras's :func:`model.fit` function.
        """
        generator = iutils.BatchSequence(X, batch_size)
        return self._fit_generator(generator,
                                  **kwargs)

    def fit_generator(self, *args, **kwargs):
        """
        Takes the same parameters as Keras's :func:`model.fit_generator`
        function.
        """
        return self._fit_generator(*args, **kwargs)

    def _fit_generator(self,
                       generator,
                       steps_per_epoch=None,
                       epochs=1,
                       max_queue_size=10,
                       workers=1,
                       use_multiprocessing=False,
                       verbose=0,
                       disable_no_training_warning=None):
        raise NotImplementedError()


class OneEpochTrainerMixin(TrainerMixin):
    """Exposes the same interface and functionality as :class:`TrainerMixin`
    except that the training is limited to one epoch.
    """

    def fit(self, *args, **kwargs):
        """
        Same interface as :func:`fit` of :class:`TrainerMixin` except that
        the parameter epoch is fixed to 1.
        """
        return super(OneEpochTrainerMixin, self).fit(*args, epochs=1, **kwargs)

    def fit_generator(self, *args, **kwargs):
        """
        Same interface as :func:`fit_generator` of :class:`TrainerMixin` except that
        the parameter epoch is fixed to 1.
        """
        steps = kwargs.pop("steps", None)
        return super(OneEpochTrainerMixin, self).fit_generator(
            *args,
            steps_per_epoch=steps,
            epochs=1,
            **kwargs)


###############################################################################
###############################################################################
###############################################################################


class AnalyzerNetworkBase(AnalyzerBase):
    """Convenience interface for analyzers.

    This class provides helpful functionality to create analyzer's.
    Basically it:

    * takes the input model and adds a layer that selects
      the desired output neuron to analyze.
    * passes the new model to :func:`_create_analysis` which should
      return the analysis as Keras tensors.
    * compiles the function and serves the output to :func:`analyze` calls.
    * allows :func:`_create_analysis` to return tensors
      that are intercept for debugging purposes.

    :param allow_lambda_layers: Allow the model to contain lambda layers.
    """

    def __init__(self, model,
                 allow_lambda_layers=False,
                 **kwargs):

        self._allow_lambda_layers = allow_lambda_layers
        self._analyzed = False
        self._add_model_check(
            lambda layer: (not self._allow_lambda_layers and
                           isinstance(layer, keras_layers.Lambda)),
            ("Lamda layers are not allowed. "
             "To force use set allow_lambda_layers parameter."),
            check_type="exception",
        )


        super(AnalyzerNetworkBase, self).__init__(model, **kwargs)

    def _add_model_softmax_check(self):
        """
        Adds check that prevents models from containing a softmax.
        """
        self._add_model_check(
            lambda layer: kchecks.contains_activation(
                layer, activation="softmax"),
            "This analysis method does not support softmax layers.",
            check_type="exception",
        )

    def create_analyzer_model(self):
        """
        Creates the analyze functionality. If not called beforehand
        it will be called by :func:`analyze`.
        """

        self._analyzer_model = self._create_analysis(self._model)

    def _create_analysis(self, model):
        """
        Interface that needs to be implemented by a derived class.

        This function is expected to create a custom analysis for the model inputs given the model outputs.

        :param model: Target of analysis.
        :return: reversed "model" as a list of input layers and a list of wrapped layers
        """
        raise NotImplementedError()

    def _handle_debug_output(self, debug_values):
        raise NotImplementedError()

    def analyze(self, X, neuron_selection="max_activation", explained_layer_names=None, stop_mapping_at_layers=None, r_init=None, f_init=None):
        """
        Takes an array-like input X and explains it. Also applies postprocessing to the explanation

        :param X: tensor or np.array of Input to be explained. Shape (n_ins, batch_size, ...) in model has multiple inputs, or (batch_size, ...) otherwise
        :param neuron_selection: neuron_selection parameter. Used to only compute explanation w.r.t. specific output neurons. One of the following:
                - None or "all"
                - "max_activation"
                - int
                - list or np.array of int, with length equal to batch size
        :param explained_layer_names: None or "all" or list of layer names whose explanations should be returned.
                                      Can be used to obtain intermediate explanations or explanations of multiple layers
                                      if layer names provided, a dictionary is returned
        :param stop_mapping_at_layers: None or list of layers to stop mapping at ("output" layers)
        :param r_init: None or Scalar or Array-Like or Dict {layer_name:scalar or array-like} reverse initialization value. Value with which the explanation is initialized.
        :param f_init: None or Scalar or Array-Like or Dict {layer_name:scalar or array-like} forward initialization value. Value with which the forward is initialized.

        :returns Dict of the form {layer name (string): explanation (numpy.ndarray)}
        """
        if not hasattr(self, "_analyzer_model"):
            self.create_analyzer_model()

        if isinstance(explained_layer_names, list):
            for l in explained_layer_names:
                if not isinstance(l, str):
                    raise AttributeError("Parameter explained_layer_names has to be None or a list of strings")
        elif explained_layer_names is not None:
            # not list and not None
            raise AttributeError("Parameter explained_layer_names has to be None or a list of strings")

        if isinstance(stop_mapping_at_layers, list):
            for l in stop_mapping_at_layers:
                if not isinstance(l, str):
                    raise AttributeError("Parameter stop_mapping_at_layers has to be None or a list of strings")
        elif stop_mapping_at_layers is not None:
            # not list and not None
            raise AttributeError("Parameter stop_mapping_at_layers has to be None or a list of strings")

        ret = self._analyzer_model.apply(X,
                                        neuron_selection=neuron_selection,
                                        explained_layer_names=explained_layer_names,
                                        stop_mapping_at_layers=stop_mapping_at_layers,
                                        r_init=r_init,
                                        f_init = f_init
                                        )
        self._analyzed=True
        ret = self._postprocess_analysis(ret)

        return ret

    def _postprocess_analysis(self, hm):
        return hm


    def get_explanations(self, explained_layer_names=None):

        """
        Get results of (previously computed) explanation.
        explanation of layer i has shape equal to input_shape of layer i.

        :param explained_layer_names: None or "all" or list of strings containing the names of the layers.
                            if explained_layer_names == 'all', explanations of all layers are returned.

        :returns Dict of the form {layer name (string): explanation (numpy.ndarray)}

        """

        if not hasattr(self, "_analyzer_model"):
            self.create_analyzer_model()

        if not self._analyzed:
            raise AttributeError("You have to analyze the model before intermediate results are available!")

        if isinstance(explained_layer_names, list):
            for l in explained_layer_names:
                if not isinstance(l, str):
                    raise AttributeError("Parameter explained_layer_names has to be None or a list of strings")
        elif explained_layer_names is not None:
            # not list and not None
            raise AttributeError("Parameter explained_layer_names has to be None or a list of strings")

        hm = self._analyzer_model.get_explanations(explained_layer_names)
        hm = self._postprocess_analysis(hm)

        return hm

class ReverseAnalyzerBase(AnalyzerNetworkBase):
    """Convenience class for analyzers that revert the model's structure.
    This class contains many helper functions around the graph
    reverse function :func:`innvestigate.utils.keras.graph.reverse_model`.
    The deriving classes should specify how the graph should be reverted.
    """

    def __init__(self,
                 model,
                 **kwargs):

        super(ReverseAnalyzerBase, self).__init__(model, **kwargs)

    def _gradient_reverse_mapping(self):
        return reverse_map.GradientReplacementLayer

    def _reverse_mapping(self, layer):
        """
        This function should return a reverse mapping for the passed layer.

        If this function returns None, :func:`_default_reverse_mapping`
        is applied.

        :param layer: The layer for which a mapping should be returned.
        :return: The mapping can be of the following forms:
          * A :class:`ReplacementLayer` subclass.
        """

        return self._apply_conditional_reverse_mappings(layer)

    def _add_conditional_reverse_mapping(
            self, condition, mapping, priority=-1, name=None):
        """
        This function should return a reverse mapping for the passed layer.

        If this function returns None, :func:`_default_reverse_mapping`
        is applied.

        :param condition: Condition when this mapping should be applied.
          Form: f(layer) -> bool
        :param mapping: The mapping can be of the following forms:
          * A function of form f(layer) that returns
            a class:`ReverseMappingBase` subclass..
          * A :class:`ReverseMappingBase` subclass.
        :param priority: The higher the earlier the condition gets
          evaluated.
        :param name: An identifying name.
        """
        if getattr(self, "_reverse_mapping_applied", False):
            raise Exception("Cannot add conditional mapping "
                            "after first application.")

        if not hasattr(self, "_conditional_reverse_mappings"):
            self._conditional_reverse_mappings = {}

        if priority not in self._conditional_reverse_mappings:
            self._conditional_reverse_mappings[priority] = []

        tmp = {"condition": condition, "mapping": mapping, "name": name}
        self._conditional_reverse_mappings[priority].append(tmp)

    def _apply_conditional_reverse_mappings(self, layer):
        mappings = getattr(self, "_conditional_reverse_mappings", {})
        self._reverse_mapping_applied = True

        # Search for mapping. First consider ones with highest priority,
        # inside priority in order of adding.
        sorted_keys = sorted(mappings.keys())[::-1]
        for key in sorted_keys:
            for mapping in mappings[key]:
                if mapping["condition"](layer):
                    if (inspect.isclass(mapping["mapping"]) and issubclass(mapping["mapping"], reverse_map.ReplacementLayer)):
                        return mapping["mapping"]
                    elif callable(mapping["mapping"]):
                        return mapping["mapping"](layer)

        return None

    def _default_reverse_mapping(self, layer):
        """
        Fallback function to map layer
        """
        return reverse_map.GradientReplacementLayer

    def _create_analysis(self, model):
        analyzer_model = reverse_map.ReverseModel(
            model,
            reverse_mappings=self._reverse_mapping,
            default_reverse_mapping=self._default_reverse_mapping,
        )

        return analyzer_model